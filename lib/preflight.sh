#!/usr/bin/env bash
version_at_least() { [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" == "$2" ]]; }
check_docker() {
    require_commands docker
    local engine compose endpoint platform
    [[ -z ${DOCKER_HOST:-} || "$DOCKER_HOST" == unix://* ]] || fatal 'Only a local Docker daemon is supported'
    endpoint=$(timeout 15 docker context inspect --format '{{.Endpoints.docker.Host}}')
    [[ -n ${DOCKER_HOST:-} || "$endpoint" == unix://* ]] || fatal 'Docker context must use a local Unix socket'
    engine=$(timeout 15 docker version --format '{{.Server.Version}}')
    compose=$(timeout 15 docker compose version --short)
    version_at_least "$engine" 24.0.0 || fatal 'Installer requires Docker Engine 24.0+'
    version_at_least "${compose#v}" 2.24.0 || fatal 'Installer requires Docker Compose plugin 2.24+'
    platform=$(timeout 15 docker info --format '{{.OSType}}/{{.Architecture}}')
    [[ "$platform" == linux/x86_64 || "$platform" == linux/amd64 ]] || fatal 'Docker must run on local Linux amd64'
    info "Docker Engine $engine; Compose $compose"
}
check_network() {
    local url code
    for url in https://pyrus.com/ https://dc.pyrus.com/ https://cr.yandex/v2/; do
        code=$(curl --silent --show-error --location --output /dev/null --write-out '%{http_code}' --connect-timeout 8 --max-time 25 "$url") || fatal "Cannot establish trusted HTTPS to $url"
        [[ "$code" =~ ^(2[0-9][0-9]|3[0-9][0-9]|401|403|404)$ ]] || fatal "Unexpected HTTP $code from $url"
        info "HTTPS $url: HTTP $code (connectivity only)"
    done
}
run_preflight() {
    local resume=$1 os_version os_id cpu ram disk path
    # shellcheck source=/etc/os-release
    source /etc/os-release
    os_id=$ID; os_version=$VERSION_ID
    [[ "$os_id" == ubuntu && ( "$os_version" == 22.04 || "$os_version" == 24.04 ) ]] || fatal 'v0.1 supports Ubuntu 22.04/24.04 only'
    [[ "$(uname -m)" == x86_64 ]] || fatal 'v0.1 supports amd64 only'
    version_at_least "$(uname -r | cut -d- -f1)" 5.4.0 || fatal 'Kernel 5.4+ required'
    cpu=$(nproc)
    ram=$(awk '/MemTotal:/ {printf "%d", $2/1024/1024}' /proc/meminfo)
    path=$INSTALL_DIR
    while [[ ! -e "$path" ]]; do path=$(dirname -- "$path"); done
    disk=$(df -BG --output=avail "$path" | tail -1 | tr -dc '0-9')
    info "Ubuntu $os_version; CPU $cpu; RAM ${ram}GiB; available disk ${disk}GiB at $path"
    if ((cpu < 8 || ram < 30 || disk < 300)); then
        warn 'Reference sizing for 100 users: 8 CPU / 32 GB RAM / 300 GB SSD.'
        [[ "$ACCEPT_LOW_RESOURCES" == true ]] || fatal 'For a disposable test only, explicitly pass --accept-low-resources'
    fi
    check_docker
    if [[ "$resume" == false ]]; then
        python3 "$ROOT_DIR/scripts/host.py" clean-host
        [[ ! -e "$INSTALL_DIR" || -z "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit)" ]] || fatal 'Install directory must be empty'
    fi
    check_network
}
check_dns_smtp() {
    getent ahosts "$PYRUS_HOSTNAME" >/dev/null || fatal 'Pyrus hostname does not resolve on this server'
    info "DNS $PYRUS_HOSTNAME resolves; confirm the A/AAAA records point to this server or its NAT."
    python3 "$ROOT_DIR/scripts/host.py" connect "$SMTP_SERVER" "$SMTP_PORT"
    info 'SMTP TCP connection works; authentication and delivery require a test in Pyrus after installation.'
}
