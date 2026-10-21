#!/usr/bin/env bash
# Runtime parameters are read from Pyrus, never from the installation snapshot.
parameter_name() { python3 "$ROOT_DIR/scripts/parameters.py" name "$1"; }
parameter_is_public() { python3 "$ROOT_DIR/scripts/parameters.py" public "$1"; }
parameter_id_valid() { python3 "$ROOT_DIR/scripts/parameters.py" id "$1" 2>/dev/null; }

read_parameter_secret() {
    local target=$1 prompt=$2 terminal_state read_status=0
    terminal_state=$(stty -g) || return 1
    # Disable echo before printing the prompt, including for pasted answers.
    trap 'stty "$terminal_state"; exit 130' INT
    trap 'stty "$terminal_state"; exit 143' TERM
    stty -echo || { trap - INT TERM; return 1; }
    printf '%s' "$prompt"
    IFS= read -r "${target?}" || read_status=$?
    stty "$terminal_state"
    trap - INT TERM
    printf '\n'
    return "$read_status"
}

show_parameter() {
    local id=$1
    if ! parameter_is_public "$id"; then
        confirm 'Значение может содержать пароль, ключ или лицензию. Показать в терминале? Введите yes' || return 0
    fi
    # Do not send runtime output through install logs: it can contain secrets
    # already scrubbed from secrets.json. stderr may also contain credentials.
    if ! setup --setup -ls -id "$id" 2>/dev/null; then
        warn 'Не удалось прочитать параметр. Проверьте ID и доступность Pyrus.'
    fi
}

show_public_parameters() {
    local id
    for id in 100000 100001 101301 101302 101401 101501 101502 101503 101554 101555 110007; do
        printf '\n%s — %s\n' "$id" "$(parameter_name "$id")"
        if parameter_is_public "$id"; then
            if ! setup --setup -ls -id "$id" 2>/dev/null; then
                warn 'Не удалось прочитать параметр.'
            fi
        else
            printf '[скрыто; используйте просмотр по ID]\n'
        fi
    done
}

edit_parameter() {
    local id=$1 value repeated
    printf '%s — %s\n' "$id" "$(parameter_name "$id")"
    if parameter_is_public "$id"; then
        IFS= read -r -p 'Новое значение (Enter — отмена): ' value || return 0
    else
        read_parameter_secret value 'Новое значение, ввод скрыт (Enter — отмена): ' || return 0
        [[ -n "$value" ]] || return 0
        read_parameter_secret repeated 'Повторите значение: ' || return 0
        [[ "$value" == "$repeated" ]] || { warn 'Значения не совпадают; запись отменена.'; return 0; }
    fi
    [[ -n "$value" ]] || return 0
    if ! printf '%s' "$value" | python3 "$ROOT_DIR/scripts/parameters.py" validate "$id"; then return 0; fi
    if [[ "$id" == 100001 ]]; then
        if [[ -z ${TLS_CERT:-} ]]; then
            warn 'TLS-сертификат не найден. Для изменения домена укажите --menu --tls-cert /path/fullchain.pem.'
            return 0
        fi
        require_commands openssl
        if ! openssl verify -partial_chain -trusted "$TLS_CERT" -verify_hostname "$value" "$TLS_CERT" >/dev/null 2>&1; then
            warn 'Установленный TLS-сертификат не покрывает новый домен или недействителен. Сначала обновите сертификат.'
            return 0
        fi
        info 'Проверьте DNS нового домена до применения.'
    fi
    if parameter_is_public "$id"; then printf 'Будет записано: %s = %s\n' "$id" "$value";
    else printf 'Будет записано: %s = [скрыто]\n' "$id"; fi
    confirm 'Сохранить параметр в Pyrus? Введите yes' || return 0
    # Suppress *all* CLI output, including on failure and for unknown parameters.
    # Values stay out of shell history, logs and saved installer secrets.
    if setup --setup -id "$id" -v "$value" >/dev/null 2>&1; then
        if [[ ${PARAMETERS_EXISTING:-false} != true ]]; then
            if ! printf '%s' "$value" | python3 "$ROOT_DIR/scripts/parameters.py" sync "$id" "$STATE_DIR/config.json"; then
                warn 'Параметр записан в Pyrus, но снимок установщика не обновлён. Проверьте config.json перед --check.'
            fi
        fi
        : >"$STATE_DIR/parameters-restart.required"
        info 'Параметр сохранён. Для применения может потребоваться перезапуск сервисов (пункт 5).'
    else
        warn 'Команда записи завершилась ошибкой. Проверьте текущее значение перед повтором.'
    fi
}

restart_parameter_services() {
    if [[ ${PARAMETERS_EXISTING:-false} == true ]]; then
        python3 "$ROOT_DIR/scripts/existing.py" restart "$EXISTING_TARGET"
    else dc restart --no-deps "$@"; fi
}

parameters_menu() {
    local choice id service application_services=()
    for service in "${RUNTIME_SERVICES[@]}"; do
        case "$service" in postgres|logs|elastic|nats|minio) continue ;; esac
        application_services+=("$service")
    done
    while true; do
        cat <<'EOF'

Конфигурация Pyrus Datacenter
  1. Посмотреть основные параметры (секреты скрыты)
  2. Посмотреть параметр по ID
  3. Изменить параметр по ID
  4. Посмотреть всю конфигурацию, включая секреты
  5. Перезапустить сервисы приложения
  0. Выход
EOF
        [[ ! -f "$STATE_DIR/parameters-restart.required" ]] || info 'После изменения параметров сервисы ещё не перезапускались.'
        read -r -p 'Выберите пункт: ' choice || return 0
        case "$choice" in
            1) show_public_parameters ;;
            2|3)
                read -r -p 'ID параметра (Enter — назад): ' id || return 0
                [[ -n "$id" ]] || continue
                if ! parameter_id_valid "$id"; then warn 'Введите положительный числовой ID параметра.'; continue; fi
                if [[ "$choice" == 2 ]]; then show_parameter "$id"; else edit_parameter "$id"; fi
                ;;
            4)
                if confirm 'Вывод содержит пароли, ключи и лицензию. Показать в терминале? Введите yes'; then
                    if ! setup --setup -ls 2>/dev/null; then warn 'Не удалось прочитать конфигурацию Pyrus.'; fi
                fi
                ;;
            5)
                if confirm 'Перезапустить сервисы приложения? Pyrus будет временно недоступен. Введите yes'; then
                    if restart_parameter_services "${application_services[@]}" >/dev/null 2>&1; then
                        rm -f -- "$STATE_DIR/parameters-restart.required"
                        info 'Сервисы перезапущены. Проверьте состояние контейнеров и выполните вход в Pyrus.'
                    else warn 'Перезапуск завершился ошибкой. Проверьте состояние контейнеров.'; fi
                fi
                ;;
            0) return 0 ;;
            *) warn 'Выберите пункт от 0 до 5.' ;;
        esac
    done
}

manage_parameters() {
    [[ -t 0 && -t 1 ]] || fatal '--menu requires an interactive terminal for input and output'
    require_commands stty
    if [[ ! -e "$STATE_DIR" && ! -L "$STATE_DIR" ]]; then manage_existing_parameters; return; fi
    [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fatal 'Invalid installer state directory'
    [[ -z "$MENU_PROJECT$MENU_TLS_CERT$MENU_SETUP_IMAGE" ]] || fatal 'Managed installation: use --install-dir with --menu; existing-deployment options are not needed'
    PARAMETERS_EXISTING=false
    require_commands sha256sum
    python3 "$ROOT_DIR/scripts/config.py" check-private "$STATE_DIR"
    acquire_lock
    load_saved_config
    [[ -f "$STATE_DIR/complete.done" ]] || fatal 'Installation is incomplete; use --resume first'
    check_docker
    verify_artifacts
    verify_database_identity postgres pyrusdb
    verify_database_identity logs pyrus_logs
    database_has_schema || fatal 'Pyrus database has no application tables'
    parameters_menu
}

manage_existing_parameters() (
    # A subshell keeps temporary metadata and overrides out of installation mode.
    trap 'exit "$?"' ERR
    require_commands docker mktemp
    PARAMETERS_EXISTING=true
    local endpoint choice count lock_file
    [[ -z ${DOCKER_HOST:-} || "$DOCKER_HOST" == unix://* ]] || fatal 'Only a local Docker daemon is supported'
    if endpoint=$(timeout 15 docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null); then
        [[ "$endpoint" == unix://* || ( -n ${DOCKER_HOST:-} && -z ${DOCKER_CONTEXT:-} ) ]] || fatal 'Docker context must use a local Unix socket'
    else
        [[ -z ${DOCKER_CONTEXT:-} || "$DOCKER_CONTEXT" == default ]] || fatal 'Cannot verify the selected Docker context'
    fi
    timeout 15 docker info >/dev/null 2>&1 || fatal 'Cannot connect to local Docker daemon'
    EXISTING_MENU_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pyrus-menu.XXXXXXXX")
    trap 'rm -rf -- "$EXISTING_MENU_DIR"' EXIT
    STATE_DIR=$EXISTING_MENU_DIR
    EXISTING_TARGET="$STATE_DIR/target.json"
    python3 "$ROOT_DIR/scripts/existing.py" discover "$STATE_DIR/projects.json" "$MENU_PROJECT"
    count=$(python3 "$ROOT_DIR/scripts/existing.py" count "$STATE_DIR/projects.json")
    ((count > 0)) || fatal 'No existing Pyrus containers found. Check docker ps -a; --menu does not install Pyrus.'
    python3 "$ROOT_DIR/scripts/existing.py" list "$STATE_DIR/projects.json"
    choice=1
    if ((count > 1)); then
        read -r -p 'Номер установки (0 — выход): ' choice || return 0
        [[ "$choice" != 0 ]] || return 0
    fi
    python3 "$ROOT_DIR/scripts/existing.py" select "$STATE_DIR/projects.json" "$choice" "$EXISTING_TARGET" "$MENU_SETUP_IMAGE" "$MENU_TLS_CERT"
    lock_file=$(python3 "$ROOT_DIR/scripts/existing.py" lock "$EXISTING_TARGET" "${PYRUS_MENU_RUNTIME_DIR:-/run/pyrus-parameters}")
    exec 9>"$lock_file"
    flock -n 9 || fatal 'Another parameter menu is using this Pyrus installation'
    TLS_CERT=$(python3 "$ROOT_DIR/scripts/existing.py" field "$EXISTING_TARGET" certificate)
    RUNTIME_SERVICES=()
    info 'Подключение к существующему Pyrus через Docker; docker-compose и docker compose для этого режима не требуются.'
    info 'Исходные Compose-файлы и версия Pyrus сохраняются. Секреты читаются из работающего контейнера только на время команды.'
    parameters_menu
)
