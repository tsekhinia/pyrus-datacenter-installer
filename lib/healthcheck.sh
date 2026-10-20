#!/usr/bin/env bash
run_healthcheck() {
    [[ -n ${LOG_FILE:-} ]] || LOG_FILE=/dev/null
    local service
    for service in "${RUNTIME_SERVICES[@]}"; do wait_for "$service" 900 service_ready "$service"; done
    verify_database_identity postgres pyrusdb
    verify_database_identity logs pyrus_logs
    database_has_schema || fatal 'Pyrus database has no application tables'
    local curl_tls=()
    [[ -z "$TLS_CA" ]] || curl_tls=(--cacert "$TLS_CA")
    # SNI, hostname, expiry and chain are checked; target local nginx to avoid a NAT hairpin.
    wait_for 'Local HTTPS certificate and application response' 300 \
        curl --fail --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 15 \
        --resolve "$PYRUS_HOSTNAME:443:127.0.0.1" "${curl_tls[@]}" --output /dev/null "https://$PYRUS_HOSTNAME/"
    quiet_run setup --version
    dc ps
    info "All enabled containers and local HTTPS passed: https://$PYRUS_HOSTNAME/"
    info 'From a client workstation: check login, SMTP code delivery and file upload/download.'
}
