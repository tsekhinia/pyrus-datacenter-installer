#!/usr/bin/env python3
"""Metadata and validation for live setup parameters, without parsing CLI output.

Only explicitly public IDs are shown without confirmation. New/unknown IDs and
storage configurations are treated as secrets, regardless of their names.
CLI contract: https://pyrus.com/ru/help/datacenter/parameters
"""
import re
import sys

import config

# ID: (Pyrus name, public, installer snapshot key). Secrets are never persisted.
PARAMETERS = {
    '100000': ('LICENSE', False, None),
    '100001': ('HOSTNAME', True, 'PYRUS_HOSTNAME'),
    '101301': ('SMTP_SERVER', True, 'SMTP_SERVER'),
    '101302': ('SMTP_PORT', True, 'SMTP_PORT'),
    '101401': ('SMTP_USE_SSL', True, 'SMTP_SSL'),
    '101501': ('SMTP_CREDENTIALS', True, 'SMTP_AUTH'),
    '101502': ('SMTP_USERNAME', True, 'SMTP_USERNAME'),
    '101503': ('SMTP_PASSWORD', False, None),
    '101554': ('SMTP_NOTIFIER_ADDRESS', True, 'SMTP_NOTIFIER'),
    '101555': ('SMTP_LOST_PASSWORD_ADDRESS', True, 'SMTP_RECOVERY'),
    '110007': ('PREVIEW_GENERATOR_TYPE', True, None),
    '9900301': ('DadataToken', False, None),
    '1040000': ('DiadocClientId', False, None),
}


def valid_id(value):
    return bool(re.fullmatch(r'[1-9][0-9]{0,9}', value)) and int(value) <= 2147483647


def validate(identifier, value):
    if not valid_id(identifier):
        raise ValueError('Некорректный ID параметра')
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Значение должно быть непустой строкой без управляющих символов')
    if identifier == '100001' and not re.fullmatch(
            r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z0-9-]{2,63}', value):
        raise ValueError('Укажите DNS-имя без https:// и пути')
    if identifier == '101301' and not re.fullmatch(r'[a-zA-Z0-9.-]+', value):
        raise ValueError('Некорректное имя SMTP-сервера')
    if identifier == '101302' and (not re.fullmatch(r'[0-9]{1,5}', value) or not 1 <= int(value) <= 65535):
        raise ValueError('SMTP-порт должен быть от 1 до 65535')
    if identifier in {'101401', '101501'} and value not in {'true', 'false'}:
        raise ValueError('Допустимые значения: true, false')
    if identifier in {'101554', '101555'} and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise ValueError('Некорректный адрес отправителя')
    if identifier == '110007' and value not in {'0', '1'}:
        raise ValueError('Допустимые значения: 0 (LibreOffice), 1 (Chrome)')


def sync(identifier, value, path):
    validate(identifier, value)
    key = PARAMETERS.get(identifier, ('', False, None))[2]
    if key:
        config.private(path)
        saved = config.read(path)
        saved[key] = value
        config.write(path, saved)


def main():
    action, identifier, *args = sys.argv[1:]
    if not valid_id(identifier):
        raise ValueError('Некорректный ID параметра')
    if action == 'id':
        return
    if action == 'name':
        print(PARAMETERS.get(identifier, ('Другой параметр (возможно, секрет)', False, None))[0])
    elif action == 'public':
        sys.exit(0 if PARAMETERS.get(identifier, ('', False, None))[1] else 1)
    elif action == 'validate':
        validate(identifier, sys.stdin.read())
    elif action == 'sync':
        sync(identifier, sys.stdin.read(), args[0])
    else:
        raise ValueError('Неизвестная операция')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print('Ошибка параметра: ' + str(error), file=sys.stderr)
        sys.exit(1)
