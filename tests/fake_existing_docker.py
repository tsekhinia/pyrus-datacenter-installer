#!/usr/bin/env python3
"""Docker API boundary for existing-installation tests; no real daemon involved."""
import json
import os
from pathlib import Path
import sys

args = sys.argv[1:]
model_file = Path(os.environ['MOCK_EXISTING_MODEL'])
model = json.loads(model_file.read_text())
record = {'args': args}
if args and args[0] == 'run':
    record['env'] = {key: os.environ[key] for key in ['APPLICATION_NAME', 'MAIN_CONN_STRING',
                     'PRIVATE_LOGS_CONN_STRING', 'PUBLIC_LOGS_CONN_STRING'] if key in os.environ}
with open(os.environ['MOCK_EXISTING_CALLS'], 'a') as log:
    log.write(json.dumps(record) + '\n')

if args[:2] == ['context', 'inspect']:
    if model.get('old_docker'):
        sys.exit(1)
    print(model.get('endpoint', 'unix:///var/run/docker.sock'))
elif args == ['info']:
    print('Local Docker')
elif args[:2] == ['ps', '-aq']:
    print('\n'.join(c['Id'] for c in model['containers']))
elif args[0] == 'inspect':
    lookup = {c['Id']: c for c in model['containers']}
    if any(identifier not in lookup for identifier in args[1:]):
        sys.exit(1)
    print(json.dumps([lookup[identifier] for identifier in args[1:]]))
elif args[:2] == ['image', 'inspect']:
    match = model['images'].get(args[2])
    if match is None:
        sys.exit(1)
    print(json.dumps([match]))
elif args[0] == 'run':
    if model.get('fail_run'):
        print('do-not-print-database-secret', file=sys.stderr)
        sys.exit(9)
    parameters = model.setdefault('parameters', {})
    if '-ls' in args:
        if '-id' in args:
            identifier = args[args.index('-id') + 1]
            print(identifier + '=' + parameters.get(identifier, '<unset>'))
        else:
            print(json.dumps(parameters))
    elif '-v' in args:
        parameters[args[args.index('-id') + 1]] = args[args.index('-v') + 1]
        model_file.write_text(json.dumps(model))
        print(args[-1])  # Deliberate echo: production must suppress write output.
    else:
        sys.exit(7)
elif args[0] == 'restart':
    print('restarted')
else:
    print('Forbidden test command', file=sys.stderr)
    sys.exit(8)
