#!/usr/bin/env bash
configure_smtp() {
    if [[ -f "$STATE_DIR/smtp.done" ]]; then return; fi
    set_parameter 101301 "$SMTP_SERVER"
    set_parameter 101302 "$SMTP_PORT"
    set_parameter 101401 "$SMTP_SSL"
    set_parameter 101501 "$SMTP_AUTH"
    if [[ "$SMTP_AUTH" == true ]]; then
        set_parameter 101502 "$SMTP_USERNAME"
        set_parameter 101503 "$SMTP_PASSWORD"
    fi
    set_parameter 101554 "$SMTP_NOTIFIER"
    set_parameter 101555 "$SMTP_RECOVERY"
    # Do not dump --setup -ls: the output may contain passwords and the license.
    mark_done smtp
}
