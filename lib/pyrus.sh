#!/usr/bin/env bash
UPSTREAM_URL=https://pyrus.com/datacenter/1.27.0/docker-compose.yml
UPSTREAM_SHA256=c2183ea98e4a6341ae5b492df32677190a71e6ac75eaa8a58d06ac1bfc37fb1d
prepare_compose() {
    if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then verify_artifacts; runtime_services; return; fi
    info 'Downloading the verified Pyrus 1.27.0 compose file'
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 120 \
        "$UPSTREAM_URL" -o "$STATE_DIR/upstream.yml.tmp"
    [[ "$(sha256sum "$STATE_DIR/upstream.yml.tmp" | cut -d' ' -f1)" == "$UPSTREAM_SHA256" ]] || fatal 'Official Compose changed. Stop: this installer supports the reviewed 1.27.0 snapshot only. See README.'
    mv "$STATE_DIR/upstream.yml.tmp" "$STATE_DIR/upstream.yml"
    quiet_run normalize_compose
    (cd "$INSTALL_DIR" && sha256sum docker-compose.yml) >"$STATE_DIR/compose.sha256"
    runtime_services
}
normalize_compose() {
    docker compose --project-name pyrus --project-directory "$INSTALL_DIR" \
        --env-file "$STATE_DIR/compose.env" -f "$STATE_DIR/upstream.yml" --profile all config --format json >"$STATE_DIR/upstream.json"
    python3 "$ROOT_DIR/scripts/compose.py" build "$STATE_DIR/upstream.json" "$STATE_DIR" "$INSTALL_DIR/docker-compose.yml"
    dc config --quiet
}
verify_artifacts() {
    python3 "$ROOT_DIR/scripts/config.py" check-private "$INSTALL_DIR/docker-compose.yml" "$STATE_DIR/compose.env" "$STATE_DIR/cert.crt" "$STATE_DIR/key.key"
    (cd "$INSTALL_DIR" && sha256sum --check --status "$STATE_DIR/compose.sha256") || fatal 'Managed Compose changed; manual edits require review before resume'
    runtime_services
}
configure_database_passwords() {
    local service database
    for service in postgres logs; do
        [[ "$service" == postgres ]] && database=pyrusdb || database=pyrus_logs
        verify_database_identity "$service" "$database" true
        # Apply on every incomplete run: deterministic password survives crashes between databases.
        # Generated hex only; stdin prevents exposing the password in the exec argv.
        [[ "$DB_PASSWORD" =~ ^[a-f0-9]{64}$ ]] || fatal 'Invalid saved database password'
        printf "SET log_statement = 'none';\nSET log_min_duration_statement = -1;\nALTER ROLE pyrusadm PASSWORD '%s';\n" "$DB_PASSWORD" |
            quiet_run dc_exec "$service" psql -X -U pyrusadm -d "$database" -v ON_ERROR_STOP=1
        # Prove the new credentials work over TCP (not merely local trust authentication).
        # shellcheck disable=SC2016
        printf '%s\n' "$DB_PASSWORD" | quiet_run dc_exec "$service" sh -c \
            'read -r PGPASSWORD; export PGPASSWORD; exec psql -X -h 127.0.0.1 -U pyrusadm -d "$1" -v ON_ERROR_STOP=1 -c "SELECT 1"' sh "$database"
    done
    info 'Both database credentials were changed and verified over TCP.'
}
verify_database_identity() {
    local service=$1 database=$2 allow_create=${3:-false} actual file
    file="$STATE_DIR/database-$service.identity"
    actual=$(dc_exec "$service" psql -X -U pyrusadm -d "$database" -At -v ON_ERROR_STOP=1 -c "SELECT system_identifier::text || ':' || (SELECT oid::text FROM pg_database WHERE datname = current_database()) FROM pg_control_system()")
    [[ "$actual" =~ ^[0-9]+:[0-9]+$ ]] || fatal 'Cannot establish PostgreSQL identity'
    if [[ -f "$file" ]]; then
        [[ "$actual" == "$(cat "$file")" ]] || fatal "Database $service was replaced; refusing to reuse installation state"
    elif [[ "$allow_create" == true && ! -f "$STATE_DIR/db_init.done" ]]; then
        printf '%s\n' "$actual" >"$file.tmp"
        mv "$file.tmp" "$file"
    else
        fatal "Missing database identity for $service; inspect state manually"
    fi
}
database_has_schema() {
    local count
    count=$(dc_exec postgres psql -X -U pyrusadm -d pyrusdb -At -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
    [[ "$count" =~ ^[0-9]+$ ]] && ((count > 0))
}
initialize_pyrus() {
    if [[ -f "$STATE_DIR/db_init.done" ]]; then
        database_has_schema || fatal 'Recorded initialized database has no application tables; refusing reinitialization'
    else
        [[ -n "$ADMIN_PASSWORD" ]] || fatal 'Administrator password is absent; inspect saved state'
        guarded_step db_init setup --create --skip --setadmin --email "$ADMIN_EMAIL" --adminpassword "$ADMIN_PASSWORD"
        database_has_schema || fatal 'Database initialization returned success but no application tables found'
        python3 "$ROOT_DIR/scripts/config.py" forget-admin "$STATE_DIR/secrets.json"
        unset ADMIN_PASSWORD
    fi
    guarded_step db_start setup -ct --start
    guarded_step elastic_init dc run --rm --no-deps -T elastic-console -iall
}
set_parameter() {
    [[ $# == 2 && -n "$2" ]] || fatal 'Refusing a setup parameter without its value'
    quiet_run setup --setup -id "$1" -v "$2"
}
configure_pyrus() {
    if [[ -f "$STATE_DIR/parameters.done" ]]; then return; fi
    set_parameter 100000 "$LICENSE"
    set_parameter 100001 "$PYRUS_HOSTNAME"
    mark_done parameters
}
start_pyrus() {
    info 'Starting application services; the first warm-up can take several minutes.'
    quiet_run dc up -d "${RUNTIME_SERVICES[@]}"
}
