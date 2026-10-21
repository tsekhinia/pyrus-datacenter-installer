#!/usr/bin/env bash
dc() {
    timeout 1800 docker compose --project-name pyrus --project-directory "$INSTALL_DIR" \
        --env-file "$STATE_DIR/compose.env" --file "$INSTALL_DIR/docker-compose.yml" "$@"
}
dc_exec() { dc exec -T "$@"; }
setup() {
    if [[ ${PARAMETERS_EXISTING:-false} == true ]]; then
        python3 "$ROOT_DIR/scripts/existing.py" setup "$EXISTING_TARGET" "$@"
    else dc run --rm --no-deps -T pyrus-setup "$@"; fi
}
service_ready() {
    local id status
    id=$(dc ps -q "$1")
    [[ -n "$id" ]] || return 1
    status=$(timeout 15 docker inspect --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$id")
    [[ "$status" == 'running healthy' || "$status" == 'running no-healthcheck' ]]
}
start_infrastructure() {
    quiet_run dc up -d postgres logs elastic nats
    wait_for PostgreSQL 300 dc_exec postgres pg_isready -U pyrusadm -d pyrusdb
    wait_for 'Logs PostgreSQL' 300 dc_exec logs pg_isready -U pyrusadm -d pyrus_logs
    local service
    for service in postgres logs elastic nats; do wait_for "$service" 600 service_ready "$service"; done
}
pull_images() {
    if [[ -f "$STATE_DIR/images.done" ]]; then return; fi
    info 'Pulling Pyrus images (can take several minutes).'
    quiet_run dc pull "${RUNTIME_SERVICES[@]}" pyrus-setup elastic-console
    # Resolve every pulled tag to its immutable repository digest before use.
    quiet_run python3 "$ROOT_DIR/scripts/compose.py" pin "$INSTALL_DIR/docker-compose.yml"
    (cd "$INSTALL_DIR" && sha256sum docker-compose.yml) >"$STATE_DIR/compose.sha256"
    mark_done images
}
runtime_services() {
    mapfile -t RUNTIME_SERVICES < <(python3 "$ROOT_DIR/scripts/compose.py" services "$INSTALL_DIR/docker-compose.yml")
    ((${#RUNTIME_SERVICES[@]} >= 15)) || fatal 'Invalid runtime service set'
}
