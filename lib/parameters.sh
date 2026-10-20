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
        if ! printf '%s' "$value" | python3 "$ROOT_DIR/scripts/parameters.py" sync "$id" "$STATE_DIR/config.json"; then
            warn 'Параметр записан в Pyrus, но снимок установщика не обновлён. Проверьте config.json перед --check.'
        fi
        : >"$STATE_DIR/parameters-restart.required"
        info 'Параметр сохранён. Для применения может потребоваться перезапуск сервисов (пункт 5).'
    else
        warn 'Команда записи завершилась ошибкой. Проверьте текущее значение перед повтором.'
    fi
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
                    if dc restart --no-deps "${application_services[@]}" >/dev/null 2>&1; then
                        rm -f -- "$STATE_DIR/parameters-restart.required"
                        info 'Сервисы перезапущены. Проверьте их командой --check и выполните вход в Pyrus.'
                    else warn 'Перезапуск завершился ошибкой. Проверьте состояние контейнеров командой --check.'; fi
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
    [[ -d "$STATE_DIR" && ! -L "$STATE_DIR" ]] || fatal 'No saved installation; run the installer first'
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
