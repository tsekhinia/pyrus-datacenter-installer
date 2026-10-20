#!/usr/bin/env bash
prepare_installation() {
    # mkdir is the exclusive ownership claim; never adopt an existing state directory.
    [[ ! -L "$INSTALL_DIR" ]] || fatal 'Install directory cannot be a symlink'
    install -d -m 700 -- "$INSTALL_DIR"
    mkdir -m 700 -- "$STATE_DIR"
    DB_PASSWORD=$(openssl rand -hex 32)
    MINIO_USER="pyrus$(openssl rand -hex 8)"
    MINIO_PASSWORD=$(openssl rand -hex 32)
    config_json | python3 "$ROOT_DIR/scripts/config.py" save "$STATE_DIR"
    install -m 600 -- "$TLS_CERT" "$STATE_DIR/cert.crt"
    install -m 600 -- "$TLS_KEY" "$STATE_DIR/key.key"
    TLS_CERT="$STATE_DIR/cert.crt"; TLS_KEY="$STATE_DIR/key.key"
    if [[ -n "$TLS_CA" ]]; then
        install -m 600 -- "$TLS_CA" "$STATE_DIR/ca.crt"
        TLS_CA="$STATE_DIR/ca.crt"
    fi
    # Persist managed paths so a resume is independent of the original files.
    config_json | python3 "$ROOT_DIR/scripts/config.py" save "$STATE_DIR"
    printf 'MINIO_ROOT_USER=%s\nMINIO_ROOT_PASSWORD=%s\n' "$MINIO_USER" "$MINIO_PASSWORD" >"$STATE_DIR/compose.env"
}
prepare_system() {
    local current
    current=$(sysctl -n vm.max_map_count)
    if ((current < 262144)); then
        [[ ! -L /etc/sysctl.d/99-pyrus-installer.conf ]] || fatal 'Unexpected sysctl symlink'
        if [[ -f /etc/sysctl.d/99-pyrus-installer.conf ]]; then
            [[ "$(cat /etc/sysctl.d/99-pyrus-installer.conf)" == 'vm.max_map_count=262144' ]] || fatal 'Existing sysctl file differs; review it manually'
        fi
        printf 'vm.max_map_count=262144\n' >/etc/sysctl.d/99-pyrus-installer.conf
        quiet_run sysctl -p /etc/sysctl.d/99-pyrus-installer.conf
    fi
    (( $(sysctl -n vm.max_map_count) >= 262144 )) || fatal 'vm.max_map_count was not applied'
}
