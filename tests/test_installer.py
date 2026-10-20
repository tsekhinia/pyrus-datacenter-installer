#!/usr/bin/env python3
"""Offline safety/contract tests plus the actual installer flow with a fake daemon.

Set COMPOSE_BINARY to a real standalone Docker Compose V2 binary to also validate
generated deployments using Docker's parser. No Pyrus license is required.
"""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from test_parameters import terminal_run

ROOT = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / (name + '.py'))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

config = module('config')
compose = module('compose')
FIXTURE = ROOT / 'tests/fixtures/upstream-1.27.0.json'

def valid_config():
    return json.loads((ROOT / 'templates/installer.json.example').read_text())

class ConfigTests(unittest.TestCase):
    def test_valid(self): config.validate(valid_config())

    def test_invalid(self):
        for key, value in [('PYRUS_HOSTNAME','https://pyrus.example.org'), ('PYRUS_HOSTNAME','-bad.example.org'),
                           ('SMTP_PORT','65536'), ('SMTP_SSL','yes'), ('ADMIN_PASSWORD','short'),
                           ('SMTP_PASSWORD','line1\nline2'), ('ADMIN_EMAIL','x@example.org\nBcc: x')]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                c=valid_config(); c[key]=value; config.validate(c)

    def test_external_s3(self):
        c=valid_config(); c.update(STORAGE_TYPE='external', S3_ENDPOINT='https://storage.yandexcloud.net',
                                   S3_ACCESS_KEY='test-key', S3_SECRET_KEY='test-secret', S3_STORAGE_TYPE='Yandex')
        config.validate(c)
        for endpoint in ['https://u:p@example.org', 'file:///etc/passwd', 'https://s3.example.org/bucket', 'https://s3.example.org?q=1']:
            c['S3_ENDPOINT']=endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError): config.validate(c)

    def test_no_shell_evaluation_and_private_input(self):
        with tempfile.TemporaryDirectory() as d:
            d=Path(d); p=d/'input.json'; marker=d/'must-not-exist'
            c=valid_config(); c['SMTP_PASSWORD']="'$(touch " + str(marker) + ')`id`$HOME\\quote"'
            config.write(p,c)
            script='source ./install.sh; load_config "$1"; printf "%s" "$SMTP_PASSWORD"'
            r=subprocess.run(['bash','-c',script,'test',str(p)],cwd=ROOT,text=True,capture_output=True)
            self.assertEqual(r.returncode,0,r.stderr); self.assertEqual(r.stdout,c['SMTP_PASSWORD'])
            self.assertFalse(marker.exists())
            p.chmod(0o644)
            with self.assertRaises(ValueError): config.private(p)

    def test_saved_secrets_do_not_override_storage_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'secrets.json'; config.write(p,{'SMTP_PASSWORD':'xyz'})
            r=subprocess.check_output(['python3',str(ROOT/'scripts/config.py'),'load-secrets',str(p),'STORAGE_TYPE','SMTP_PASSWORD'])
            self.assertEqual(r,b'\0xyz\0')

    def test_scrub_and_redact(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'secrets.json'; c=dict(ADMIN_PASSWORD='admin-secret-123',LICENSE='license-secret-123',DB_PASSWORD='a'*64,MINIO_USER='minio-test',MINIO_PASSWORD='minio-secret')
            config.write(p,c)
            raw=' '.join(c.values())
            r=subprocess.run(['python3',str(ROOT/'scripts/redact.py'),str(p)],input=raw,text=True,capture_output=True,check=True)
            self.assertNotIn('admin-secret',r.stdout); self.assertNotIn('license-secret',r.stdout)
            subprocess.run(['python3',str(ROOT/'scripts/config.py'),'scrub',str(p)],check=True)
            self.assertEqual(set(config.read(p)),{'DB_PASSWORD','MINIO_USER','MINIO_PASSWORD'})

class ComposeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name)
        self.c=valid_config(); config.write(self.state/'config.json',self.c)
        self.secrets=dict(DB_PASSWORD='d'*64,MINIO_USER='generated-user',MINIO_PASSWORD='e'*64)
        config.write(self.state/'secrets.json',self.secrets)
        self.upstream=json.loads(FIXTURE.read_text())

    def model(self): return compose.build(copy.deepcopy(self.upstream),self.state)

    def test_connections_credentials_tls_and_optional_services(self):
        m=self.model(); s=m['services']
        for service in s.values():
            for k,v in service.get('environment',{}).items():
                if k.endswith('_CONN_STRING'):
                    self.assertIn('Password='+'d'*64+';',json.loads(v)['ConnectionString'])
                    self.assertNotIn('pyruspwd',v)
        self.assertEqual(s['postgres']['environment']['POSTGRES_PASSWORD'],'d'*64)
        self.assertEqual(s['logs']['environment']['POSTGRES_PASSWORD'],'d'*64)
        self.assertNotIn('clickhouse',s['pyrus-setup']['depends_on'])
        self.assertNotIn('profiles',s['minio'])
        self.assertEqual(s['goskey']['profiles'],['installer-disabled'])
        self.assertEqual(s['clickhouse']['profiles'],['installer-disabled'])
        self.assertTrue(all(v['read_only'] for v in s['nginx']['volumes']))
        self.assertEqual(s['web-api']['healthcheck'],self.upstream['services']['web-api']['healthcheck'])

    def test_external_s3_disables_local_minio(self):
        self.c['STORAGE_TYPE']='external'; config.write(self.state/'config.json',self.c)
        self.assertEqual(self.model()['services']['minio']['profiles'],['installer-disabled'])

    def test_upstream_contract_change_fails_closed(self):
        for change in ['service','version','password']:
            m=copy.deepcopy(self.upstream)
            if change=='service': m['services']['new-service']={}
            if change=='version': m['services']['web-api']['image']='cr.yandex/crpn0l4dp22f8mv5ln18/pyrus-web-api:1.28.0'
            if change=='password': m['services']['web-api']['environment']['MAIN_CONN_STRING']='{}'
            with self.subTest(change=change), self.assertRaises((ValueError,KeyError)): compose.build(m,self.state)

    @unittest.skipUnless(os.environ.get('COMPOSE_BINARY'),'Set COMPOSE_BINARY for real Compose parser validation')
    def test_real_compose_parser(self):
        for storage in ['minio','external']:
            self.c['STORAGE_TYPE']=storage; config.write(self.state/'config.json',self.c)
            p=self.state/'compose.yml'; compose.write(p,self.model())
            r=subprocess.run([os.environ['COMPOSE_BINARY'],'-f',str(p),'--profile','all','config','--format','json'],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr)
            model=json.loads(r.stdout)
            self.assertIn('pyrus-setup',model['services'])
            self.assertNotIn('clickhouse',model['services'])
            self.assertNotIn('goskey',model['services'])
            self.assertEqual('minio' in model['services'],storage=='minio')

class FlowTests(unittest.TestCase):
    """Runs main(), swapping only host probes/confirmation and external executables."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name); self.bin=self.base/'bin'; self.bin.mkdir()
        self.install=self.base/'pyrus'; self.call_log=self.base/'calls.jsonl'
        self.config=valid_config()
        # Actual OpenSSL verifies matching certificate/key and hostname during main().
        cert=self.base/'cert.pem'; key=self.base/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-keyout',str(key),'-out',str(cert),'-subj','/CN=pyrus.example.org','-addext','subjectAltName=DNS:pyrus.example.org'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        self.config.update(TLS_CERT=str(cert),TLS_KEY=str(key),TLS_CA=str(cert))
        self.input=self.base/'input.json'; config.write(self.input,self.config)
        for name in ['docker','curl','ss']:
            p=self.bin/name; p.symlink_to(ROOT/'tests/fake_command.py')
        self.env=os.environ | dict(PATH=str(self.bin)+':'+os.environ['PATH'], MOCK_CALLS=str(self.call_log),MOCK_STATE=str(self.base/'db.json'),MOCK_FIXTURE=str(FIXTURE),MOCK_SOURCE=str(ROOT/'tests/fixtures/upstream-1.27.0.yml'))

    def run_flow(self,*args,extra_env=None):
        # Overrides prevent kernel/network changes. Production main() and all stages run.
        harness='''source ./install.sh
require_root() { :; }
run_preflight() { check_docker; }
prepare_system() { info "Test: kernel change suppressed"; }
check_dns_smtp() { :; }
confirm() { return 0; }
main "$@"
'''
        return subprocess.run(['bash','-c',harness,'test','--install-dir',str(self.install),*args],cwd=ROOT,env=self.env | (extra_env or {}),text=True,capture_output=True,timeout=50)

    def calls(self): return [json.loads(x) for x in self.call_log.read_text().splitlines()]

    def run_menu_flow(self, *args, exchanges=(), extra_env=None):
        harness='source ./install.sh; require_root() { :; }; main "$@"'
        return terminal_run(['bash', '-c', harness, 'test', '--install-dir', str(self.install), *args],
                            self.env | (extra_env or {}), exchanges)

    def test_menu_and_automatic_entry_use_completed_installation(self):
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        state=self.install/'.installer'
        before_files={p:p.read_bytes() for p in [state/'config.json',state/'secrets.json',self.install/'docker-compose.yml']}
        log_files=set(state.glob('*.log'))
        for args in [('--menu',), ()]:
            before=len(self.calls())
            code,output=self.run_menu_flow(*args,exchanges=[('Выберите пункт: ','0')])
            self.assertEqual(code,0,output)
            self.assertIn('Конфигурация Pyrus',output)
            self.assertFalse(any('--setup' in c or '--create' in c or 'restart' in c or 'up' in c or 'pull' in c for c in self.calls()[before:]))
            self.assertEqual(set(state.glob('*.log')),log_files)
        self.assertEqual(before_files,{p:p.read_bytes() for p in before_files})

    def test_menu_rejects_incomplete_installation_and_conflicting_modes(self):
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        (self.install/'.installer/complete.done').unlink()
        before=len(self.calls())
        code,output=self.run_menu_flow('--menu')
        self.assertNotEqual(code,0); self.assertIn('use --resume first',output)
        self.assertEqual(len(self.calls()),before)
        for args in [('--menu','--plan'), ('--menu','--config',str(self.input))]:
            r=self.run_flow(*args)
            self.assertNotEqual(r.returncode,0)
        self.assertEqual(len(self.calls()),before)

    def test_menu_rejects_redirected_output_and_changed_compose(self):
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        before=len(self.calls())
        r=self.run_flow('--menu')
        self.assertNotEqual(r.returncode,0); self.assertIn('interactive terminal',r.stderr)
        self.assertEqual(len(self.calls()),before)
        with (self.install/'docker-compose.yml').open('a') as f: f.write(' ')
        code,output=self.run_menu_flow('--menu')
        self.assertNotEqual(code,0); self.assertIn('Managed Compose changed',output)
        self.assertFalse(any('--setup' in c for c in self.calls()[before:]))
    def mutations(self,calls):
        return [c for c in calls if any(x in c for x in ['pull','up','exec','--create','--configure-s3','--setup'])]

    def test_install_resume_and_secrets(self):
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        state=self.install/'.installer'
        self.assertTrue((state/'complete.done').exists())
        secrets=config.read(state/'secrets.json')
        self.assertNotIn('ADMIN_PASSWORD',secrets); self.assertNotIn('LICENSE',secrets)
        for p in [state/'config.json',state/'secrets.json',self.install/'docker-compose.yml',state/'key.key']:
            self.assertEqual(p.stat().st_mode & 0o777,0o600)
        self.assertNotIn(self.config['ADMIN_PASSWORD'],r.stdout+r.stderr)
        self.assertNotIn(self.config['SMTP_PASSWORD'],''.join(p.read_text() for p in state.glob('*.log')))
        before=len(self.calls()); r=self.run_flow('--resume'); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        fresh=self.calls()[before:]
        self.assertFalse(any('--create' in c or '--setup' in c or '--configure-s3' in c or 'up' in c or 'pull' in c for c in fresh))
        self.assertFalse(any('clickhouse' in c or 'goskey' in c for c in self.calls() if 'up' in c or 'pull' in c))
        self.assertTrue(all('--no-deps' in c for c in self.calls() if 'run' in c))

    def test_interrupted_database_requires_explicit_retry(self):
        r=self.run_flow('--config',str(self.input),extra_env={'MOCK_FAIL_CREATE':'1'})
        self.assertNotEqual(r.returncode,0); self.assertTrue((self.install/'.installer/db_init.running').exists())
        before=len(self.calls()); r=self.run_flow('--resume')
        self.assertNotEqual(r.returncode,0); self.assertIn('inspect database/index state first',r.stderr)
        self.assertFalse(any('--create' in c for c in self.calls()[before:]))
        r=self.run_flow('--resume','--retry-step','db_init'); self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def test_failure_at_smtp_resumes_without_db_reinitialization(self):
        r=self.run_flow('--config',str(self.input),extra_env={'MOCK_FAIL_SMTP':'1'})
        self.assertNotEqual(r.returncode,0)
        before=len(self.calls()); r=self.run_flow('--resume'); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(any('--create' in c or '-iall' in c for c in self.calls()[before:]))

    def test_changed_compose_and_database_are_rejected(self):
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        db=self.base/'db.json'; original=db.read_text(); db.write_text('{"schema": true, "identity": "999:777"}')
        r=self.run_flow('--resume'); self.assertNotEqual(r.returncode,0); self.assertIn('was replaced',r.stderr)
        db.write_text(original)
        with (self.install/'docker-compose.yml').open('a') as f: f.write(' ')
        r=self.run_flow('--resume'); self.assertNotEqual(r.returncode,0); self.assertIn('Managed Compose changed',r.stderr)

    def test_plan_writes_nothing_and_fresh_run_refuses_adoption(self):
        r=self.run_flow('--plan','--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertFalse(self.install.exists()); self.assertFalse(self.mutations(self.calls()))
        r=self.run_flow('--config',str(self.input)); self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        before=len(self.calls()); r=self.run_flow('--config',str(self.input))
        self.assertNotEqual(r.returncode,0); self.assertIn('Installation exists',r.stderr)
        self.assertFalse(self.mutations(self.calls()[before:]))

    def test_mismatched_tls_and_changed_upstream_stop(self):
        c=valid_config(); c.update(self.config); c['PYRUS_HOSTNAME']='other.example.org'; config.write(self.input,c)
        r=self.run_flow('--plan','--config',str(self.input))
        self.assertNotEqual(r.returncode,0); self.assertFalse(self.install.exists())
        config.write(self.input,self.config)
        r=self.run_flow('--config',str(self.input),extra_env={'MOCK_BAD_UPSTREAM':'1'})
        self.assertNotEqual(r.returncode,0); self.assertIn('Official Compose changed',r.stderr)
        self.assertFalse(any('up' in c or 'pull' in c for c in self.calls()))

    def test_remote_docker_context_is_rejected(self):
        r=self.run_flow('--plan','--config',str(self.input),extra_env={'MOCK_DOCKER_ENDPOINT':'ssh://server'})
        self.assertNotEqual(r.returncode,0); self.assertIn('local Unix socket',r.stderr)
        self.assertFalse(self.install.exists())

    def test_clean_host_probe_refuses_existing_pyrus_resources(self):
        host=module('host')
        with patch.object(host.subprocess,'check_output',return_value='pyrus-old\n'):
            with self.assertRaisesRegex(ValueError,'Existing Pyrus'): host.check_resources()

    def test_unhealthy_service_has_bounded_wait(self):
        script='source ./install.sh; LOG_FILE=/dev/null; STATE_DIR="$1"; service_ready() { return 1; }; wait_for test-service 0 service_ready'
        # Redactor needs a secrets file even for an empty command output.
        config.write(self.base/'secrets.json',{})
        r=subprocess.run(['bash','-c',script,'test',str(self.base)],cwd=ROOT,env=self.env,text=True,capture_output=True,timeout=5)
        self.assertNotEqual(r.returncode,0); self.assertIn('Timeout waiting',r.stderr)

if __name__=='__main__': unittest.main(verbosity=2)
