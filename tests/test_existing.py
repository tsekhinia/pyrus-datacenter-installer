"""Existing Pyrus management with an old Docker CLI and no installer state."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_parameters import terminal_run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import config
import existing

DB_SECRET = 'Server=pyrus-postgresql;Password=db-secret-$()";Database=pyrusdb;'


def deployment(project='old-pyrus'):
    def container(identifier, name, reference):
        return {'Id': project + '-' + identifier, 'Image': 'sha256:' + identifier,
                'Config': {'Image': reference, 'Labels': {'com.docker.compose.project': project,
                    'com.docker.compose.service': name, 'com.docker.compose.oneoff': 'False'},
                    'Env': ['MAIN_CONN_STRING=' + json.dumps({'ConnectionString': DB_SECRET}, indent=2),
                            'PRIVATE_LOGS_CONN_STRING={"ConnectionString":"private-db-secret"}']},
                'State': {'Running': True},
                'NetworkSettings': {'Networks': {'backend': {'NetworkID': project + '-network'}}},
                'Mounts': []}
    return [container('web', 'web-api', 'registry.example/pyrus-web-api:1.26.1'),
            container('db', 'postgres', 'postgres:14'),
            container('logs', 'logs', 'postgres:14'),
            container('nginx', 'nginx', 'nginx:stable'),
            container('minio', 'minio', 'minio/minio'),
            container('unrelated', 'custom-database', 'example/database')]


class ExistingPolicyTests(unittest.TestCase):
    def test_discovery_stores_no_secrets_and_ignores_other_projects_and_oneoffs(self):
        containers = deployment() + deployment('another-pyrus')
        containers[0]['Config']['Labels']['com.docker.compose.oneoff'] = 'True'
        def call(*args):
            if args[0] == 'ps': return ' '.join(c['Id'] for c in containers)
            return json.dumps(containers)
        with patch.object(existing, 'docker', side_effect=call):
            result = existing.discover()
        self.assertEqual([r['project'] for r in result], ['another-pyrus'])
        self.assertNotIn('ConnectionString', json.dumps(result))

    def test_ambiguous_network_fails_and_missing_certificate_does_not_block_other_parameters(self):
        containers = deployment()
        self.assertEqual(existing.certificate(containers, ''), '')
        containers[0]['NetworkSettings']['Networks']['second'] = {'NetworkID': 'second'}
        containers[1]['NetworkSettings']['Networks']['second'] = {'NetworkID': 'second'}
        with self.assertRaisesRegex(ValueError, 'одна общая'):
            existing.network_id(containers[0], containers[1])

    def test_installer_operations_are_never_accepted_by_existing_adapter(self):
        for args in [['--create'], ['--update'], ['--configure-s3'], ['--setup'], ['--setup', '-id', '1;id', '-v', 'x']]:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'только чтение'):
                existing.run_setup({}, args)

    def test_changed_container_identity_prevents_setup(self):
        containers=deployment()
        target={'anchor':'old-pyrus-web', 'database':'old-pyrus-db', 'project':'old-pyrus',
                'anchor_image':'sha256:web', 'database_image':'sha256:previous',
                'network':'old-pyrus-network'}
        with patch.object(existing, 'inspect', return_value=containers[:2]), self.assertRaisesRegex(ValueError, 'изменились'):
            existing.run_setup(target, ['--setup', '-ls'])

    def test_selected_metadata_has_no_database_credentials(self):
        containers=deployment()
        project={'project':'old-pyrus', 'containers':[c['Id'] for c in containers], 'anchors':[containers[0]['Id']]}
        with patch.object(existing, 'inspect', return_value=containers), patch.object(existing, 'image', return_value={'Id':'sha256:setup'}):
            target=existing.select(project)
        self.assertNotIn('MAIN_CONN_STRING', json.dumps(target))
        self.assertNotIn('db-secret', json.dumps(target))

    def test_dev_digest_reference_resolves_local_tag_without_changing_registry(self):
        anchor=deployment()[0]
        anchor['Config']['Image']='registry.example:5000/team/dev-pyrus-web-api@sha256:web'
        def local_image(reference):
            if reference == anchor['Image']:
                return {'RepoTags':['registry.example:5000/team/dev-pyrus-web-api:1.27.0']}
            self.assertEqual(reference,'registry.example:5000/team/dev-pyrus-setup:1.27.0')
            return {'Id':'sha256:dev-setup'}
        with patch.object(existing,'image',side_effect=local_image):
            identifier,reference=existing.setup_image(anchor,'')
        self.assertEqual(identifier,'sha256:dev-setup')
        self.assertEqual(reference,'registry.example:5000/team/dev-pyrus-setup:1.27.0')

    def test_lock_rejects_insecure_directory_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'locks'
            target = {'project': 'pyrus', 'database': 'db'}
            first = Path(existing.lock_path(target, path))
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
            first.unlink()
            victim = Path(directory) / 'victim'
            victim.write_text('keep')
            first.symlink_to(victim)
            with self.assertRaises(OSError): existing.lock_path(target, path)
            self.assertEqual(victim.read_text(), 'keep')
            path.chmod(0o777)
            with self.assertRaises(ValueError): existing.lock_path(target, path)


class ExistingMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bin = self.base / 'bin'; self.bin.mkdir()
        (self.bin / 'docker').symlink_to(ROOT / 'tests/fake_existing_docker.py')
        self.tmp = self.base / 'tmp'; self.tmp.mkdir()
        self.model_file = self.base / 'model.json'
        self.model = {'old_docker': True, 'containers': deployment(),
                      'images': {'registry.example/pyrus-setup:1.26.1': {'Id': 'sha256:setup'},
                                 'sha256:setup': {'Id': 'sha256:setup'}},
                      'parameters': {'101301': 'smtp-live.example.org'}}
        self.calls_file = self.base / 'calls.jsonl'
        self.install = self.base / 'not-an-installer-directory'
        self.env = os.environ | dict(PATH=str(self.bin) + ':' + os.environ['PATH'], TMPDIR=str(self.tmp),
                                     MOCK_EXISTING_MODEL=str(self.model_file), MOCK_EXISTING_CALLS=str(self.calls_file),
                                     PYRUS_MENU_RUNTIME_DIR=str(self.base / 'locks'))
        for key in ['DOCKER_HOST', 'DOCKER_CONTEXT', 'PUBLIC_LOGS_CONN_STRING']:
            self.env.pop(key, None)

    def run_menu(self, exchanges=(), args=(), env=None):
        config.write(self.model_file, self.model) if not self.model_file.exists() else self.model_file.write_text(json.dumps(self.model))
        return terminal_run(['bash', '-c', 'source ./install.sh; require_root() { :; }; main "$@"',
                             'test', '--install-dir', str(self.install), '--menu', *args],
                            self.env | (env or {}), exchanges)

    def calls(self):
        return [json.loads(line) for line in self.calls_file.read_text().splitlines()]

    def test_legacy_docker_without_either_compose_command_lists_live_parameters(self):
        code, output = self.run_menu([('Выберите пункт: ', '2'), ('ID параметра (Enter — назад): ', '101301'),
                                      ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('smtp-live.example.org', output)
        self.assertIn('pyrus-setup:1.26.1', output)
        self.assertNotIn(DB_SECRET, output)
        self.assertFalse(self.install.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])
        runs = [c for c in self.calls() if c['args'][0] == 'run']
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertEqual(run['args'][-5:], ['sha256:setup', '--setup', '-ls', '-id', '101301'])
        self.assertEqual(json.loads(run['env']['MAIN_CONN_STRING'])['ConnectionString'], DB_SECRET)
        self.assertNotIn(DB_SECRET, json.dumps(run['args']))
        self.assertNotIn('PUBLIC_LOGS_CONN_STRING', run['env'])
        self.assertFalse(any(c['args'][0] in ['compose', 'pull', 'up', 'create'] for c in self.calls()))

    def dev_deployment(self):
        self.model['containers'] = deployment('ubuntu')
        self.model['containers'][0]['Config']['Image'] = 'registry.example/dev-pyrus-web-api:1.27.0'
        self.model['images'] = {'registry.example/dev-pyrus-setup:1.27.0': {'Id': 'sha256:dev-setup'},
                                'sha256:dev-setup': {'Id': 'sha256:dev-setup'}}

    def test_dev_images_are_discovered_and_use_the_matching_dev_setup(self):
        self.dev_deployment()
        code, output = self.run_menu([('Выберите пункт: ', '2'), ('ID параметра (Enter — назад): ', '101301'),
                                      ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('Pyrus: "ubuntu"', output)
        self.assertIn('dev-pyrus-setup:1.27.0', output)
        self.assertIn('smtp-live.example.org', output)
        runs = [c['args'] for c in self.calls() if c['args'][0] == 'run']
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0][-5:], ['sha256:dev-setup', '--setup', '-ls', '-id', '101301'])
        self.assertFalse(any('registry.example/pyrus-setup:1.27.0' in c['args'] for c in self.calls()))
        self.assertFalse(self.install.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_dev_setup_explicit_same_version_override_is_allowed(self):
        self.dev_deployment()
        code, output = self.run_menu([('Выберите пункт: ', '0')],
                                      args=['--setup-image', 'registry.example/dev-pyrus-setup:1.27.0'])
        self.assertEqual(code, 0, output)

    def test_dev_target_does_not_allow_a_production_setup_override(self):
        self.dev_deployment()
        self.model['images']['registry.example/pyrus-setup:1.27.0'] = {'Id': 'sha256:production-setup'}
        code, output = self.run_menu(args=['--setup-image', 'registry.example/pyrus-setup:1.27.0'])
        self.assertNotEqual(code, 0)
        self.assertIn('не совпадает', output)
        self.assertFalse(any(c['args'][0] == 'run' for c in self.calls()))

    def test_secret_write_and_restart_do_not_reinitialize_or_touch_infrastructure(self):
        secret = ' smtp-secret-$()`quote" '
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '101503'),
                                      ('(Enter — отмена): ', secret), ('Повторите значение: ', secret),
                                      ('Введите yes: ', 'yes'), ('Выберите пункт: ', '5'),
                                      ('Введите yes: ', 'yes'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertNotIn(secret, output)
        self.assertEqual(json.loads(self.model_file.read_text())['parameters']['101503'], secret)
        self.assertEqual([c['args'] for c in self.calls() if c['args'][0] == 'restart'],
                         [['restart', 'old-pyrus-web', 'old-pyrus-nginx']])
        self.assertFalse(self.install.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertFalse(any('--create' in c['args'] or '--update' in c['args'] for c in self.calls()))
        self.assertTrue(all(DB_SECRET not in p.read_text() for p in (self.base / 'locks').iterdir()))

    def test_missing_local_image_fails_before_running_any_container(self):
        self.model['images'] = {}
        code, output = self.run_menu()
        self.assertNotEqual(code, 0)
        self.assertIn('Локальный образ pyrus-setup не найден', output)
        self.assertFalse(any(c['args'][0] in ['run', 'pull', 'restart'] for c in self.calls()))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_mismatched_setup_image_is_rejected(self):
        self.model['images']['registry.example/pyrus-setup:1.27.0'] = {'Id': 'sha256:other'}
        code, output = self.run_menu(args=['--setup-image', 'registry.example/pyrus-setup:1.27.0'])
        self.assertNotEqual(code, 0)
        self.assertIn('не совпадает с версией', output)
        self.assertFalse(any(c['args'][0] == 'run' for c in self.calls()))

    def test_multiple_projects_require_a_selection_and_explicit_filter_works(self):
        self.model['containers'] += deployment('second-pyrus')
        code, output = self.run_menu([('Номер установки (0 — выход): ', '2'), ('Выберите пункт: ', '2'),
                                      ('ID параметра (Enter — назад): ', '101301'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        runs=[c['args'] for c in self.calls() if c['args'][0] == 'run']
        self.assertEqual(runs[0][runs[0].index('--network') + 1], 'second-pyrus-network')
        code, output = self.run_menu([('Выберите пункт: ', '0')], args=['--project', 'old-pyrus'])
        self.assertEqual(code, 0, output)
        self.assertNotIn('Номер установки', output)

    def test_stopped_database_or_no_project_never_opens_an_installation(self):
        self.model['containers'][1]['State']['Running'] = False
        code, output = self.run_menu()
        self.assertNotEqual(code, 0)
        self.assertIn('работающие web-api и postgres', output)
        self.assertFalse(self.install.exists())
        self.model['containers'] = []
        code, output = self.run_menu()
        self.assertNotEqual(code, 0)
        self.assertIn('No existing Pyrus containers found', output)
        self.assertNotIn('--resume', output)
        self.assertNotIn('private log', output)
        self.assertNotIn('Data and volumes have been preserved', output)
        self.assertFalse(self.install.exists())

    def test_remote_docker_is_rejected_before_discovery(self):
        self.model['old_docker'] = False
        self.model['endpoint'] = 'ssh://server'
        code, output = self.run_menu(env={'DOCKER_CONTEXT': 'remote', 'DOCKER_HOST': 'unix:///var/run/docker.sock'})
        self.assertNotEqual(code, 0)
        self.assertIn('local Unix socket', output)
        self.assertFalse(any(c['args'][0] == 'run' for c in self.calls()))

    def test_domain_change_without_certificate_is_cancelled(self):
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '100001'),
                                      ('(Enter — отмена): ', 'pyrus.example.org'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('--tls-cert', output)
        self.assertFalse(any(c['args'][0] == 'run' for c in self.calls()))

    def test_failed_write_stays_in_menu_without_leaking_process_errors(self):
        self.model['fail_run'] = True
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '101301'),
                                      ('(Enter — отмена): ', 'smtp-new.example.org'), ('Введите yes: ', 'yes'),
                                      ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('Команда записи завершилась ошибкой', output)
        self.assertNotIn('do-not-print-database-secret', output)
        self.assertEqual(json.loads(self.model_file.read_text())['parameters']['101301'], 'smtp-live.example.org')


if __name__ == '__main__':
    unittest.main(verbosity=2)
