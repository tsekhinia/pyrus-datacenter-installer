#!/usr/bin/env python3
"""Strict JSON configuration; no shell evaluation, atomic private writes."""
import json
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit

SECRET_KEYS = {'ADMIN_PASSWORD', 'LICENSE', 'S3_ACCESS_KEY', 'S3_SECRET_KEY',
               'SMTP_PASSWORD', 'DB_PASSWORD', 'MINIO_USER', 'MINIO_PASSWORD'}
DEFAULTS = dict(TLS_CA='', STORAGE_TYPE='minio', S3_BUCKET='pyrus',
                S3_STORAGE_TYPE='MinIo', S3_FORCE_PATH_STYLE='true', SMTP_PORT='465',
                SMTP_SSL='true', SMTP_AUTH='true')

def read(path):
    obj = json.loads(Path(path).read_text())
    if not isinstance(obj, dict) or any(not isinstance(v, str) for v in obj.values()):
        raise ValueError('JSON object with string values required')
    if any('\0' in v or '\n' in v or '\r' in v for v in obj.values()):
        raise ValueError('Configuration values must be single-line strings')
    return obj

def write(path, obj):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def private(path):
    p = Path(path)
    s = p.lstat()
    if p.is_symlink() or s.st_uid != os.geteuid() or stat.S_IMODE(s.st_mode) & 0o077:
        raise ValueError('Expected owner-only file/directory: ' + str(p))

def validate(c):
    required = ['PYRUS_HOSTNAME', 'ADMIN_EMAIL', 'ADMIN_PASSWORD', 'LICENSE', 'TLS_CERT',
                'TLS_KEY', 'SMTP_SERVER', 'SMTP_NOTIFIER', 'SMTP_RECOVERY']
    if any(not c.get(k) for k in required):
        raise ValueError('Required settings: ' + ', '.join(k for k in required if not c.get(k)))
    if not re.fullmatch(r'(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z0-9-]{2,63}', c['PYRUS_HOSTNAME']):
        raise ValueError('Invalid DNS hostname')
    for k in ['ADMIN_EMAIL', 'SMTP_NOTIFIER', 'SMTP_RECOVERY']:
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', c[k]): raise ValueError('Invalid email: ' + k)
    for k in ['TLS_CERT', 'TLS_KEY', 'TLS_CA']:
        if c.get(k) and not Path(c[k]).is_absolute(): raise ValueError('Absolute path required: ' + k)
    if len(c['ADMIN_PASSWORD']) < 12: raise ValueError('Admin password must have at least 12 characters')
    for k in ['SMTP_SSL', 'SMTP_AUTH', 'S3_FORCE_PATH_STYLE']:
        if c.get(k) not in ['true', 'false']: raise ValueError('true/false required: ' + k)
    if not c.get('SMTP_PORT', '').isdigit() or not 1 <= int(c['SMTP_PORT']) <= 65535:
        raise ValueError('Invalid SMTP port')
    if not re.fullmatch(r'[a-zA-Z0-9.-]+', c['SMTP_SERVER']): raise ValueError('Invalid SMTP hostname')
    if c['SMTP_AUTH'] == 'true' and not all(c.get(k) for k in ['SMTP_USERNAME', 'SMTP_PASSWORD']):
        raise ValueError('SMTP credentials required')
    if c['STORAGE_TYPE'] not in ['minio', 'external']: raise ValueError('Storage must be minio or external')
    if c['STORAGE_TYPE'] == 'external':
        if not all(c.get(k) for k in ['S3_ENDPOINT', 'S3_BUCKET', 'S3_ACCESS_KEY', 'S3_SECRET_KEY']):
            raise ValueError('External S3 settings are incomplete')
        u = urlsplit(c['S3_ENDPOINT'])
        if u.scheme not in ['http', 'https'] or not u.hostname or u.username or u.password or u.path not in ['', '/'] or u.query or u.fragment:
            raise ValueError('Invalid S3 endpoint URL')
        if c['S3_STORAGE_TYPE'] not in ['MinIo', 'Mts', 'VkCloud', 'Yandex']:
            raise ValueError('Unsupported S3 provider')
    if any('\n' in v or '\r' in v or '\0' in v for v in c.values()):
        raise ValueError('Multiline values are not supported')

def main():
    action, *args = sys.argv[1:]
    if action == 'validate-file': private(args[0]); read(args[0])
    elif action in ['load', 'load-secrets']:
        c = (DEFAULTS if action == 'load' else {}) | read(args[0])
        for key in args[1:]: sys.stdout.buffer.write(c.get(key, '').encode() + b'\0')
    elif action == 'from-nul':
        parts = sys.stdin.buffer.read().decode().split('\0')[:-1]
        print(json.dumps(dict(zip(parts[::2], parts[1::2]))))
    elif action == 'validate': validate(json.load(sys.stdin))
    elif action == 'save':
        c = json.load(sys.stdin)
        write(Path(args[0]) / 'config.json', {k:v for k,v in c.items() if k not in SECRET_KEYS})
        write(Path(args[0]) / 'secrets.json', {k:v for k,v in c.items() if k in SECRET_KEYS})
    elif action == 'scrub':
        c = read(args[0]); write(args[0], {k:v for k,v in c.items() if k in {'DB_PASSWORD', 'MINIO_USER', 'MINIO_PASSWORD'}})
    elif action == 'forget-admin':
        c = read(args[0]); c.pop('ADMIN_PASSWORD', None); write(args[0], c)
    elif action == 'check-private':
        for path in args: private(path)
    else: raise ValueError('Unknown action')

if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError) as e:
        print('Configuration error: ' + str(e), file=sys.stderr); sys.exit(1)
