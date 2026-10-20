"""Runtime parameter validation and terminal interaction, without a Pyrus license."""
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import config
import parameters


def terminal_run(command, env, exchanges):
    """Send each answer only after its prompt, including hidden read -s input."""
    master, slave = pty.openpty()
    child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    output = b''
    pending = b''
    remaining = list(exchanges)
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                output += data
                pending += data
                while remaining and remaining[0][0].encode() in pending:
                    prompt, answer = remaining.pop(0)
                    pending = pending.split(prompt.encode(), 1)[1]
                    os.write(master, (answer + '\n').encode())
            elif child.poll() is not None:
                break
        else:
            raise AssertionError('Terminal timed out: ' + output.decode(errors='replace'))
        code = child.wait(timeout=5)
        if remaining:
            raise AssertionError('Unanswered prompts: ' + repr(remaining) + '\n' + output.decode(errors='replace'))
        return code, output.decode(errors='replace')
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)


class ParameterValidationTests(unittest.TestCase):
    def test_ids_and_types(self):
        for value in ['100001', '2147483647']:
            self.assertTrue(parameters.valid_id(value))
        for value in ['', '0', '-1', '001', '1; id', '2147483648', '١٠٠٠٠١']:
            self.assertFalse(parameters.valid_id(value))
        for identifier, value in [('101302', '65536'), ('101302', '-1'), ('101401', 'yes'),
                                  ('100001', 'https://example.org'), ('101554', 'not-mail'),
                                  ('110007', '2'), ('999999', '\x1b[2J'), ('101503', 'a\nb')]:
            with self.subTest(identifier=identifier, value=value), self.assertRaises(ValueError):
                parameters.validate(identifier, value)
        for identifier, value in [('101302', '465'), ('101401', 'false'), ('110007', '1'),
                                  ('100001', 'pyrus.example.org'), ('999999', 'a $() `x` " \\')]:
            parameters.validate(identifier, value)

    def test_sync_only_public_installer_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            config.write(path, {'PYRUS_HOSTNAME': 'old.example.org'})
            parameters.sync('100001', 'new.example.org', path)
            parameters.sync('101302', '587', path)
            for identifier in ['100000', '101503', '9911571', '999999']:
                parameters.sync(identifier, 'test-secret', path)
            self.assertEqual(config.read(path), {'PYRUS_HOSTNAME': 'new.example.org', 'SMTP_PORT': '587'})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ParameterTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        config.write(self.base / 'config.json', {'SMTP_SERVER': 'old.example.org'})
        self.calls = self.base / 'calls.jsonl'
        # Production menu, setup helper and dc wrapper; only the daemon is fake.
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        (self.bin / 'docker').symlink_to(ROOT / 'tests/fake_command.py')
        self.env = os.environ | dict(PATH=str(self.bin) + ':' + os.environ['PATH'],
                                     MOCK_CALLS=str(self.calls), MOCK_STATE=str(self.base / 'db.json'))
        self.harness = '''source ./install.sh
STATE_DIR=$1
INSTALL_DIR=$1
TLS_CERT=$1/cert.pem
RUNTIME_SERVICES=(postgres logs elastic nats minio web-api nginx)
parameters_menu
'''

    def run_menu(self, exchanges, extra_env=None):
        return terminal_run(['bash', '-c', self.harness, 'test', str(self.base)],
                            self.env | (extra_env or {}), exchanges)

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_views_do_not_mutate_and_secrets_require_confirmation(self):
        config.write(self.base / 'db.json', {'parameters': {'101301': 'live.example.org', '101503': 'hidden-pass'}})
        code, output = self.run_menu([('Выберите пункт: ', '1'), ('Выберите пункт: ', '2'),
                                      ('ID параметра (Enter — назад): ', '101503'), ('Введите yes: ', 'no'),
                                      ('Выберите пункт: ', '4'), ('Введите yes: ', 'no'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('live.example.org', output)
        self.assertNotIn('old.example.org', output)
        self.assertNotIn('hidden-pass', output)
        self.assertTrue(self.commands())
        self.assertTrue(all('-ls' in call and '-id' in call for call in self.commands()))
        self.assertFalse(any('101503' in call for call in self.commands()))

    def test_secret_update_is_hidden_and_literal(self):
        secret = ' new-secret-$()`quote"\\ '
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '101503'),
                                      ('(Enter — отмена): ', secret), ('Повторите значение: ', secret),
                                      ('Введите yes: ', 'yes'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertNotIn(secret, output)
        self.assertEqual(json.loads((self.base / 'db.json').read_text())['parameters']['101503'], secret)
        self.assertNotIn(secret, (self.base / 'config.json').read_text())
        self.assertTrue((self.base / 'parameters-restart.required').exists())
        self.assertEqual(self.commands()[0][-4:], ['-id', '101503', '-v', secret])

    def test_validation_cancellation_and_failure_keep_snapshot(self):
        for value, answer, env in [('65536', None, {}), ('587', 'no', {}), ('587', 'yes', {'MOCK_FAIL_SET': '1'})]:
            with self.subTest(value=value, answer=answer):
                exchanges = [('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '101302'),
                             ('(Enter — отмена): ', value)]
                if answer:
                    exchanges += [('Введите yes: ', answer)]
                exchanges += [('Выберите пункт: ', '0')]
                code, output = self.run_menu(exchanges, env)
                self.assertEqual(code, 0, output)
                self.assertNotIn('hidden-error-secret', output)
                self.assertEqual(config.read(self.base / 'config.json'), {'SMTP_SERVER': 'old.example.org'})
                self.assertFalse((self.base / 'parameters-restart.required').exists())

    def test_update_snapshot_and_explicit_application_restart(self):
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '101301'),
                                      ('(Enter — отмена): ', 'new.example.org'), ('Введите yes: ', 'yes'),
                                      ('Выберите пункт: ', '5'), ('Введите yes: ', 'no'),
                                      ('Выберите пункт: ', '5'), ('Введите yes: ', 'yes'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertEqual(config.read(self.base / 'config.json')['SMTP_SERVER'], 'new.example.org')
        restarts = [call for call in self.commands() if 'restart' in call]
        self.assertEqual(len(restarts), 1)
        self.assertEqual(restarts[0][restarts[0].index('restart'):], ['restart', '--no-deps', 'web-api', 'nginx'])
        self.assertFalse((self.base / 'parameters-restart.required').exists())

    def test_unknown_parameter_is_hidden_and_eof_exits(self):
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '999999'),
                                      ('(Enter — отмена): ', 'unknown-secret'), ('Повторите значение: ', 'different'),
                                      ('Выберите пункт: ', '\x04')])
        self.assertEqual(code, 0, output)
        self.assertNotIn('unknown-secret', output)
        self.assertFalse(self.commands())

    def test_rejected_id_and_listing_failure_return_to_menu(self):
        code, output = self.run_menu([('Выберите пункт: ', '2'), ('ID параметра (Enter — назад): ', '100001; echo x'),
                                      ('Выберите пункт: ', '2'), ('ID параметра (Enter — назад): ', '101301'),
                                      ('Выберите пункт: ', '0')], {'MOCK_FAIL_LIST': '1'})
        self.assertEqual(code, 0, output)
        self.assertIn('Не удалось прочитать', output)
        self.assertNotIn('hidden-error-secret', output)
        self.assertEqual(len(self.commands()), 1)

    def test_secret_view_requires_yes_and_read_failure_is_safe(self):
        config.write(self.base / 'db.json', {'parameters': {'101503': 'explicit-secret'}})
        code, output = self.run_menu([('Выберите пункт: ', '2'), ('ID параметра (Enter — назад): ', '101503'),
                                      ('Введите yes: ', 'yes'), ('Выберите пункт: ', '4'),
                                      ('Введите yes: ', 'yes'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('explicit-secret', output)
        self.assertEqual(len(self.commands()), 2)
        self.assertTrue(all('-ls' in call for call in self.commands()))
        self.assertEqual(list(self.base.glob('*.log')), [])

    def test_failed_restart_keeps_reminder(self):
        marker=self.base / 'parameters-restart.required'
        marker.touch()
        code, output = self.run_menu([('Выберите пункт: ', '5'), ('Введите yes: ', 'yes'),
                                      ('Выберите пункт: ', '0')], {'MOCK_FAIL_RESTART': '1'})
        self.assertEqual(code, 0, output)
        self.assertIn('Перезапуск завершился ошибкой', output)
        self.assertTrue(marker.exists())

    def test_domain_must_match_installed_certificate(self):
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                        '-keyout', str(self.base / 'key.pem'), '-out', str(self.base / 'cert.pem'),
                        '-subj', '/CN=pyrus.example.org', '-addext', 'subjectAltName=DNS:pyrus.example.org'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        code, output = self.run_menu([('Выберите пункт: ', '3'), ('ID параметра (Enter — назад): ', '100001'),
                                      ('(Enter — отмена): ', 'other.example.org'), ('Выберите пункт: ', '0')])
        self.assertEqual(code, 0, output)
        self.assertIn('сертификат не покрывает новый домен', output)
        self.assertFalse(self.commands())


if __name__ == '__main__':
    unittest.main(verbosity=2)
