#!/usr/bin/env python3
"""Transform the reviewed upstream model. JSON output is valid Compose YAML."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys

RUNTIME = ['postgres', 'logs', 'elastic', 'nginx', 'nats', 'preview', 'identity-server',
           'web-api', 'hocus', 'files', 'bind', 'mail-reader', 'mail-sender', 'async-worker',
           'notification-service', 'pyrus-schedule-to-today', 'familiar', 'stt-service']
TOOLS = ['pyrus-setup', 'elastic-console']
OPTIONAL = ['minio', 'goskey', 'clickhouse']

def load(path): return json.loads(Path(path).read_text())

def write(path, obj):
    tmp = str(path) + '.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(obj, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, path)

def build(model, state):
    state = Path(state)
    c = load(state / 'config.json'); secrets = load(state / 'secrets.json')
    services = model['services']
    if set(services) != set(RUNTIME + TOOLS + OPTIONAL):
        raise ValueError('Unexpected upstream services; review the new Compose contract')
    db_pass = secrets['DB_PASSWORD']
    if not re.fullmatch('[a-f0-9]{64}', db_pass): raise ValueError('Invalid generated DB credential')
    for name, s in services.items():
        if not s['image'].startswith('cr.yandex/crpn0l4dp22f8mv5ln18/'):
            raise ValueError('Unexpected image registry')
        if name != 'minio' and not s['image'].endswith(':1.27.0'):
            raise ValueError('Unsupported Pyrus version')
        if s.get('ports') and name != 'nginx': raise ValueError('Unexpected public internal service')
        s['pull_policy'] = 'missing'
        env = s.setdefault('environment', {})
        for key, value in list(env.items()):
            if key.endswith('_CONN_STRING'):
                conn = json.loads(value)
                if 'Password=pyruspwd;' not in conn['ConnectionString']:
                    raise ValueError('Unexpected database connection string')
                conn['ConnectionString'] = conn['ConnectionString'].replace('Password=pyruspwd;', f'Password={db_pass};')
                env[key] = json.dumps(conn)
        if name in OPTIONAL: s['profiles'] = ['installer-disabled']
    # The reviewed images use the standard PostgreSQL entrypoint and these variables.
    for name, database in [('postgres', 'pyrusdb'), ('logs', 'pyrus_logs')]:
        services[name]['environment'].update(POSTGRES_USER='pyrusadm', POSTGRES_DB=database, POSTGRES_PASSWORD=db_pass)
    for name in TOOLS: services[name]['profiles'] = ['all']
    services['pyrus-setup']['depends_on'].pop('clickhouse', None)
    if c['STORAGE_TYPE'] == 'minio':
        services['minio'].pop('profiles', None)
        services['minio']['environment'] = dict(MINIO_ROOT_USER=secrets['MINIO_USER'], MINIO_ROOT_PASSWORD=secrets['MINIO_PASSWORD'])
    services['nginx']['volumes'] = [
        dict(type='bind', source=str(state / 'cert.crt'), target='/etc/nginx/ssl/cert.crt', read_only=True),
        dict(type='bind', source=str(state / 'key.key'), target='/etc/nginx/ssl/key.key', read_only=True),
    ]
    # Drop the normalized obsolete version key; retain upstream healthchecks and dependency graph.
    model.pop('version', None)
    model['name'] = 'pyrus'
    return model

def pin(model):
    for name, s in model['services'].items():
        if name not in RUNTIME + TOOLS and 'profiles' in s: continue
        result = json.loads(subprocess.check_output(['docker', 'image', 'inspect', s['image']], text=True, timeout=20))[0]
        repo = s['image'].split('@')[0].split(':')[0]
        digests = [d for d in result.get('RepoDigests', []) if d.startswith(repo + '@sha256:')]
        if not digests: raise ValueError('No immutable digest for image: ' + name)
        s['image'] = digests[0]
    return model

def main():
    action, *args = sys.argv[1:]
    if action == 'build': write(args[2], build(load(args[0]), args[1]))
    elif action == 'pin': write(args[0], pin(load(args[0])))
    elif action == 'services':
        model = load(args[0]); names = list(RUNTIME)
        if 'profiles' not in model['services']['minio']: names.append('minio')
        print('\n'.join(names))
    else: raise ValueError('Unknown action')

if __name__ == '__main__':
    try: main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as e:
        print('Compose error: ' + str(e), file=sys.stderr); sys.exit(1)
