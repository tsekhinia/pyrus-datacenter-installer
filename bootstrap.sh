#!/usr/bin/env bash
# Download a complete, pinned installer bundle before executing its entry point.
set +x
set -Eeuo pipefail
umask 077

readonly INSTALLER_COMMIT=7dd2cb9c13ed50cc449a6bb416f8860ce5db27a0
readonly ARCHIVE_SHA256=38eeee329657e2cc43dbe9cd3e4536af332cf05d163e7451e1b2c418fd49f6b6
readonly ARCHIVE_URL="https://codeload.github.com/tsekhinia/pyrus-datacenter-installer/tar.gz/$INSTALLER_COMMIT"

bootstrap_error() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }

bootstrap_main() {
    local tool downloader work_dir archive bundle status=0
    for tool in mktemp tar sha256sum bash; do
        command -v "$tool" >/dev/null || bootstrap_error "Missing command: $tool"
    done
    if command -v curl >/dev/null; then downloader=curl
    elif command -v wget >/dev/null; then downloader=wget
    else bootstrap_error 'Install curl or wget first'; fi

    BOOTSTRAP_WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pyrus-installer.XXXXXXXX")
    work_dir=$BOOTSTRAP_WORK_DIR
    # Keep the complete bundle until the child exits, including on failure.
    trap 'rm -rf -- "$BOOTSTRAP_WORK_DIR"' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    archive="$work_dir/installer.tar.gz"
    bundle="$work_dir/pyrus-datacenter-installer-$INSTALLER_COMMIT"

    printf '[INFO] Downloading Pyrus installer %s\n' "$INSTALLER_COMMIT"
    if [[ "$downloader" == curl ]]; then
        curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
            --connect-timeout 15 --max-time 180 --retry 2 \
            --output "$archive" "$ARCHIVE_URL" || bootstrap_error 'Installer download failed'
    else
        wget --https-only --timeout=30 --tries=3 --output-document="$archive" \
            "$ARCHIVE_URL" || bootstrap_error 'Installer download failed'
    fi
    printf '%s  %s\n' "$ARCHIVE_SHA256" "$archive" | sha256sum --check --status ||
        bootstrap_error 'Installer checksum mismatch; nothing has been executed'
    tar -xzf "$archive" -C "$work_dir" || bootstrap_error 'Cannot extract installer bundle'
    [[ -f "$bundle/install.sh" && -d "$bundle/lib" && -d "$bundle/scripts" ]] ||
        bootstrap_error 'Incomplete installer bundle'

    # A pipe supplies the bootstrap source on stdin. Reconnect the child to the
    # controlling terminal so the wizard, hidden passwords and yes still work.
    if [[ ! -t 0 ]] && { true </dev/tty; } 2>/dev/null; then
        bash "$bundle/install.sh" "$@" </dev/tty || status=$?
    else
        bash "$bundle/install.sh" "$@" || status=$?
    fi
    return "$status"
}

# Keep this invocation last: the bundle is never executed from a partial download.
bootstrap_main "$@"
