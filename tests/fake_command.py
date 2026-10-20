#!/usr/bin/env python3
"""Small fake Docker daemon for installer orchestration tests, never used at runtime."""
import hashlib
import json
import os
from pathlib import Path
import sys

name=Path(sys.argv[0]).name
a=sys.argv[1:]
with open(os.environ['MOCK_CALLS'],'a') as f: f.write(json.dumps([name]+a)+'\n')
state_file=Path(os.environ['MOCK_STATE'])
state=json.loads(state_file.read_text()) if state_file.exists() else {'schema':False,'identity':'123456789:16384'}
if name=='ss': sys.exit(0)
if name=='curl':
    if '-o' in a:
        # The checked-in fixture is normalized; integration tests use source from
        # MOCK_SOURCE or download a reviewed source in CI before this test suite.
        source=Path(os.environ['MOCK_SOURCE'])
        Path(a[a.index('-o')+1]).write_bytes(b'changed' if os.environ.get('MOCK_BAD_UPSTREAM') else source.read_bytes())
    if '--write-out' in a: print('200',end='')
    sys.exit(0)
if name!='docker': sys.exit(2)
if a[0]=='version': print('29.1.3'); sys.exit(0)
if a[0]=='info': print('linux/x86_64'); sys.exit(0)
if a[:2]==['context','inspect']: print(os.environ.get('MOCK_DOCKER_ENDPOINT','unix:///var/run/docker.sock')); sys.exit(0)
if a[0] in ['container','volume','network']:
    if os.environ.get('MOCK_EXISTING_RESOURCES'): print('pyrus-old')
    sys.exit(0)
if a[0]=='inspect': print('running healthy'); sys.exit(0)
if a[:2]==['image','inspect']:
    repo=a[2].split('@')[0].split(':')[0]
    print(json.dumps([{'RepoDigests':[repo+'@sha256:'+hashlib.sha256(repo.encode()).hexdigest()]}])); sys.exit(0)
if a[0]=='compose':
    if a[1:]==['version','--short']: print('2.40.3'); sys.exit(0)
    # Compose global options in dc() have explicit values; preserve service args.
    x=a[1:]
    while x and x[0].startswith('-'):
        option=x.pop(0)
        if option in ['--project-name','--project-directory','--env-file','--file','-f','--profile']: x.pop(0)
    cmd=x.pop(0)
    if cmd=='config':
        if '--format' in x: print(Path(os.environ['MOCK_FIXTURE']).read_text())
    elif cmd=='ps':
        print('mock-'+x[-1] if '-q' in x else 'All mocked services healthy')
    elif cmd=='exec':
        if 'psql' in x and 'pg_control_system()' in ' '.join(x): print(state['identity'])
        elif 'psql' in x and 'information_schema.tables' in ' '.join(x): print(42 if state['schema'] else 0)
        else: sys.stdin.read()
    elif cmd=='run':
        if '--create' in x:
            if os.environ.get('MOCK_FAIL_CREATE'): print('simulated create failure'); sys.exit(8)
            state['schema']=True; state_file.write_text(json.dumps(state))
        if '--setup' in x and '101301' in x and os.environ.get('MOCK_FAIL_SMTP'): sys.exit(9)
        # Deliberately echo arguments to prove the redaction filter works.
        print(' '.join(x))
    elif cmd not in ['pull','up']: print('Unhandled compose command',cmd,file=sys.stderr); sys.exit(3)
    sys.exit(0)
print('Unhandled fake docker command',a,file=sys.stderr); sys.exit(3)
