#!/usr/bin/env bash
# Pyrus Datacenter single-host installer. Run from a reviewed checkout.
set +x
set -Eeuo pipefail
umask 077
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export ROOT_DIR
for module in common config preflight system docker pyrus storage smtp healthcheck parameters; do
    # shellcheck source=/dev/null
    source "$ROOT_DIR/lib/$module.sh"
done
trap 'on_error "$?" "$LINENO"' ERR

main() {
    local mode=install input='' arg mode_count=0
    INSTALL_DIR=/opt/pyrus
    ACCEPT_LOW_RESOURCES=false
    RETRY_STEP=
    MENU_PROJECT=
    MENU_TLS_CERT=
    MENU_SETUP_IMAGE=
    PARAMETERS_EXISTING=false
    ERROR_CONTEXT=installation
    while (($#)); do
        arg=$1; shift
        case "$arg" in
            --help|-h) usage; return ;;
            --preflight) mode=preflight; mode_count=$((mode_count + 1)) ;;
            --plan) mode=plan; mode_count=$((mode_count + 1)) ;;
            --check) mode=check; mode_count=$((mode_count + 1)) ;;
            --resume) mode=resume; mode_count=$((mode_count + 1)) ;;
            --menu) mode=menu; mode_count=$((mode_count + 1)) ;;
            --project) (($#)) || fatal 'Missing project name'; MENU_PROJECT=$1; shift ;;
            --tls-cert) (($#)) || fatal 'Missing certificate path'; MENU_TLS_CERT=$1; shift ;;
            --setup-image) (($#)) || fatal 'Missing local setup image'; MENU_SETUP_IMAGE=$1; shift ;;
            --config) (($#)) || fatal 'Missing --config value'; input=$1; shift ;;
            --install-dir) (($#)) || fatal 'Missing directory'; INSTALL_DIR=$1; shift ;;
            --accept-low-resources) ACCEPT_LOW_RESOURCES=true ;;
            --retry-step) (($#)) || fatal 'Missing step'; RETRY_STEP=$1; shift ;;
            *) fatal "Unknown argument: $arg" ;;
        esac
    done
    if ((mode_count == 0)) && [[ -n "$MENU_PROJECT$MENU_TLS_CERT$MENU_SETUP_IMAGE" ]]; then mode=menu; fi
    [[ "$mode" == menu || -z "$MENU_PROJECT$MENU_TLS_CERT$MENU_SETUP_IMAGE" ]] || fatal '--project, --tls-cert and --setup-image require --menu'
    ((mode_count <= 1)) || fatal 'Choose only one mode: --preflight, --plan, --check, --resume or --menu'
    [[ -z "$RETRY_STEP" || "$mode" == resume ]] || fatal '--retry-step requires --resume'
    [[ "$RETRY_STEP" =~ ^(db_init|db_start|elastic_init|storage)?$ ]] || fatal 'Unsupported retry step'
    require_commands python3 timeout flock
    validate_install_dir
    if ((mode_count == 0)) && [[ -z "$input" && -d "$STATE_DIR" ]]; then mode=menu; fi
    if [[ "$mode" == menu ]]; then
        ERROR_CONTEXT=menu
        [[ -z "$input" ]] || fatal '--menu uses the running Pyrus configuration; do not pass --config'
        require_root
        manage_parameters
        return
    fi
    require_commands curl openssl sha256sum ss getent
    if [[ "$mode" == preflight ]]; then run_preflight false; return; fi
    require_root
    if [[ "$mode" == resume || "$mode" == check ]]; then
        [[ -z "$input" ]] || fatal 'Resume/check uses saved configuration; do not pass --config'
        load_saved_config
        check_docker
        if [[ -f "$INSTALL_DIR/docker-compose.yml" ]]; then verify_artifacts;
        elif [[ "$mode" == check ]]; then fatal 'Compose has not been prepared yet'; fi
        if [[ "$mode" == check ]]; then run_healthcheck; return; fi
    else
        [[ ! -e "$INSTALL_DIR/.installer" ]] || fatal 'Installation exists; use --menu, --resume or --check'
        if [[ -n "$input" ]]; then load_config "$input"; else collect_configuration; fi
        validate_config
    fi
    run_preflight "$([[ "$mode" == resume ]] && echo true || echo false)"
    validate_tls
    check_dns_smtp
    show_plan
    if [[ "$mode" == plan ]]; then info 'Plan only: no files or containers changed.'; return; fi
    confirm 'Apply this installation plan? Type yes' || fatal 'Cancelled'
    if [[ "$mode" != resume ]]; then prepare_installation; fi
    acquire_lock
    start_log
    if [[ -f "$STATE_DIR/complete.done" ]]; then
        info 'Installation is complete; checking the existing deployment.'
        run_healthcheck
        python3 "$ROOT_DIR/scripts/config.py" scrub "$STATE_DIR/secrets.json"
        return
    fi
    prepare_system
    prepare_compose
    pull_images
    start_infrastructure
    configure_database_passwords
    initialize_pyrus
    configure_storage
    configure_pyrus
    configure_smtp
    start_pyrus
    run_healthcheck
    mark_done complete
    python3 "$ROOT_DIR/scripts/config.py" scrub "$STATE_DIR/secrets.json"
    info "Pyrus Datacenter is ready: https://$PYRUS_HOSTNAME/ (administrator: $ADMIN_EMAIL)"
}
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
