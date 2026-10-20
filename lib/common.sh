#!/usr/bin/env bash
usage() {
    cat <<'EOF'
Usage: sudo bash install.sh [options]
  --preflight                 Read-only host/Docker/network checks
  --plan                      Collect/validate configuration, print plan; no writes
  --config /root/pyrus.json    Read configuration JSON instead of prompting
  --install-dir /opt/pyrus     Installation directory (also for resume/check)
  --resume                    Continue the saved installation; never upgrades Pyrus
  --check                     Verify existing installation
  --accept-low-resources      Permit below recommended CPU/RAM/disk for a test
  --retry-step STEP           With --resume, explicitly retry interrupted
                              db_init, db_start, elastic_init, storage after review
EOF
}
info() { printf '[INFO] %s\n' "$*"; }
warn() { printf '[WARN] %s\n' "$*" >&2; }
fatal() { printf '[ERROR] %s\n' "$*" >&2; exit 1; }
on_error() {
    local code=$1 line=$2
    trap - ERR
    printf '[ERROR] Step failed (exit %s, installer line %s).\n' "$code" "$line" >&2
    printf 'Data and volumes have been preserved. Review the private log, then use --resume.\n' >&2
    exit "$code"
}
require_root() { [[ $EUID == 0 ]] || fatal 'Run with sudo/root'; }
require_commands() {
    local command
    for command; do command -v "$command" >/dev/null || fatal "Missing command: $command"; done
}
confirm() {
    local answer
    [[ -t 0 ]] || fatal 'Confirmation needs an interactive terminal'
    read -r -p "$1: " answer
    [[ "$answer" == yes ]]
}
acquire_lock() {
    exec 9>"$STATE_DIR/lock"
    flock -n 9 || fatal 'Another installer is running'
}
start_log() {
    LOG_FILE="$STATE_DIR/install-$(date -u +%Y%m%dT%H%M%SZ).log"
    : >"$LOG_FILE"
    chmod 600 "$LOG_FILE"
    info "Private command log: $LOG_FILE"
}
quiet_run() {
    # Never log argv: setup CLI arguments can contain secrets.
    "$@" 2>&1 | python3 "$ROOT_DIR/scripts/redact.py" "$STATE_DIR/secrets.json" >>"$LOG_FILE"
}
mark_done() {
    printf 'done\n' >"$STATE_DIR/$1.done.tmp"
    mv -- "$STATE_DIR/$1.done.tmp" "$STATE_DIR/$1.done"
    rm -f -- "$STATE_DIR/$1.running"
}
guarded_step() {
    local step=$1; shift
    if [[ -f "$STATE_DIR/$step.done" ]]; then info "Already completed: $step"; return; fi
    if [[ -f "$STATE_DIR/$step.running" && "$RETRY_STEP" != "$step" ]]; then
        fatal "Interrupted $step: inspect database/index state first; then explicitly use --resume --retry-step $step"
    fi
    : >"$STATE_DIR/$step.running"
    info "Running: $step"
    quiet_run "$@"
    mark_done "$step"
}
wait_for() {
    local label=$1 limit=$2 deadline; shift 2
    deadline=$((SECONDS + limit))
    while ! quiet_run "$@"; do
        ((SECONDS < deadline)) || fatal "Timeout waiting for $label; see private log"
        sleep 3
    done
    info "$label: ready"
}
