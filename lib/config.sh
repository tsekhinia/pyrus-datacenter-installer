#!/usr/bin/env bash
CONFIG_KEYS=(PYRUS_HOSTNAME ADMIN_EMAIL ADMIN_PASSWORD LICENSE TLS_CERT TLS_KEY TLS_CA STORAGE_TYPE S3_ENDPOINT S3_BUCKET S3_ACCESS_KEY S3_SECRET_KEY S3_STORAGE_TYPE S3_FORCE_PATH_STYLE SMTP_SERVER SMTP_PORT SMTP_SSL SMTP_AUTH SMTP_USERNAME SMTP_PASSWORD SMTP_NOTIFIER SMTP_RECOVERY DB_PASSWORD MINIO_USER MINIO_PASSWORD)
load_config() {
    local file=$1 values=() i
    python3 "$ROOT_DIR/scripts/config.py" validate-file "$file"
    mapfile -d '' -t values < <(python3 "$ROOT_DIR/scripts/config.py" load "$file" "${CONFIG_KEYS[@]}")
    ((${#values[@]} == ${#CONFIG_KEYS[@]})) || fatal 'Cannot read configuration'
    for i in "${!CONFIG_KEYS[@]}"; do printf -v "${CONFIG_KEYS[i]}" '%s' "${values[i]}"; done
}
ask() { local key=$1 label=$2 default=${3:-} value; read -r -p "$label${default:+ [$default]}: " value; printf -v "$key" '%s' "${value:-$default}"; }
ask_secret() { local key=$1 label=$2 value; read -r -s -p "$label: " value; printf '\n'; printf -v "$key" '%s' "$value"; }
collect_configuration() {
    [[ -t 0 ]] || fatal 'Use a terminal or --config /root/pyrus.json'
    local key password_again
    for key in "${CONFIG_KEYS[@]}"; do printf -v "$key" ''; done
    ask PYRUS_HOSTNAME 'Pyrus domain (no https://)'
    ask ADMIN_EMAIL 'Administrator email'
    ask_secret ADMIN_PASSWORD 'Administrator password (at least 12 characters)'
    ask_secret password_again 'Repeat administrator password'
    [[ "$ADMIN_PASSWORD" == "$password_again" ]] || fatal 'Passwords differ'
    ask_secret LICENSE 'Pyrus license'
    ask TLS_CERT 'Absolute path to PEM full certificate chain'
    ask TLS_KEY 'Absolute path to unencrypted PEM private key'
    ask TLS_CA 'Optional CA bundle for private PKI (empty for system trust)'
    ask STORAGE_TYPE 'Storage: minio or external' minio
    S3_BUCKET=pyrus; S3_STORAGE_TYPE=MinIo; S3_FORCE_PATH_STYLE=true
    if [[ "$STORAGE_TYPE" == external ]]; then
        ask S3_ENDPOINT 'S3 endpoint (reachable from Docker network)'
        ask S3_BUCKET 'S3 bucket' pyrus
        ask S3_ACCESS_KEY 'S3 access key'
        ask_secret S3_SECRET_KEY 'S3 secret key'
        ask S3_STORAGE_TYPE 'Provider: MinIo, Mts, VkCloud, Yandex' MinIo
        ask S3_FORCE_PATH_STYLE 'S3 forcePathStyle: true or false' true
    fi
    ask SMTP_SERVER 'SMTP hostname'
    ask SMTP_PORT 'SMTP port' 465
    ask SMTP_SSL 'SMTP SSL: true or false (confirm mode with mail admin)' true
    ask SMTP_AUTH 'SMTP authentication: true or false' true
    if [[ "$SMTP_AUTH" == true ]]; then
        ask SMTP_USERNAME 'SMTP username'; ask_secret SMTP_PASSWORD 'SMTP password'
    fi
    ask SMTP_NOTIFIER 'Notification sender address' "$ADMIN_EMAIL"
    ask SMTP_RECOVERY 'Password recovery sender address' "$SMTP_NOTIFIER"
}
config_json() {
    local key
    for key in "${CONFIG_KEYS[@]}"; do printf '%s\0%s\0' "$key" "${!key:-}"; done |
        python3 "$ROOT_DIR/scripts/config.py" from-nul
}
validate_install_dir() {
    [[ "$INSTALL_DIR" =~ ^/[a-zA-Z0-9_/-]+$ && "$INSTALL_DIR" != / && "$INSTALL_DIR" != */../* && "$INSTALL_DIR" != */.. && "$INSTALL_DIR" != */./* ]] || fatal 'Use a simple absolute installation path without .. or spaces'
    INSTALL_DIR=$(realpath -m -- "$INSTALL_DIR")
    [[ "$INSTALL_DIR" != / ]] || fatal 'Invalid installation directory'
    STATE_DIR="$INSTALL_DIR/.installer"
}
validate_config() { config_json | python3 "$ROOT_DIR/scripts/config.py" validate; }
load_saved_config() {
    [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fatal 'No saved installation at this directory'
    python3 "$ROOT_DIR/scripts/config.py" check-private "$STATE_DIR" "$STATE_DIR/config.json" "$STATE_DIR/secrets.json"
    load_config "$STATE_DIR/config.json"
    local values=() i
    mapfile -d '' -t values < <(python3 "$ROOT_DIR/scripts/config.py" load-secrets "$STATE_DIR/secrets.json" "${CONFIG_KEYS[@]}")
    ((${#values[@]} == ${#CONFIG_KEYS[@]})) || fatal 'Cannot read saved secrets'
    for i in "${!CONFIG_KEYS[@]}"; do [[ -z "${values[i]}" ]] || printf -v "${CONFIG_KEYS[i]}" '%s' "${values[i]}"; done
}
validate_tls() {
    [[ -f "$TLS_CERT" && -f "$TLS_KEY" ]] || fatal 'Certificate/key file not found'
    openssl x509 -in "$TLS_CERT" -noout -checkend 86400 >/dev/null || fatal 'Certificate expires within 24 hours or is invalid'
    # x509 -checkhost can exit 0 even on a mismatch. verify has reliable exit status.
    # Trust the supplied leaf only for this hostname/key precheck; final curl checks the chain.
    openssl verify -partial_chain -trusted "$TLS_CERT" -verify_hostname "$PYRUS_HOSTNAME" "$TLS_CERT" >/dev/null || fatal 'Certificate does not cover hostname or is not yet valid'
    local cert_pub key_pub
    cert_pub=$(openssl x509 -in "$TLS_CERT" -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum)
    key_pub=$(openssl pkey -in "$TLS_KEY" -passin pass: -pubout -outform DER | sha256sum)
    [[ "$cert_pub" == "$key_pub" ]] || fatal 'Certificate and private key do not match'
    [[ -z "$TLS_CA" || -f "$TLS_CA" ]] || fatal 'CA bundle does not exist'
}
show_plan() {
    printf '\nPyrus 1.27.0 / single-host / Docker Compose\nDirectory: %s\nURL: https://%s/\nAdministrator: %s\nStorage: %s\nSMTP: %s:%s (SSL=%s, auth=%s)\n' "$INSTALL_DIR" "$PYRUS_HOSTNAME" "$ADMIN_EMAIL" "$STORAGE_TYPE" "$SMTP_SERVER" "$SMTP_PORT" "$SMTP_SSL" "$SMTP_AUTH"
    info 'Creates named Docker volumes and a private network; publishes TCP 80 and 443.'
    info 'Changes vm.max_map_count if below 262144. Does not change firewall or DNS.'
}
