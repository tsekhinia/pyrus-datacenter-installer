"""Run the standalone collector with fake host/Docker commands and inspect ZIPs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FAKE = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys
import time

name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['CALLS'], 'a') as output:
    output.write(json.dumps([name] + args) + '\n')
mode = os.environ.get('SCENARIO', '')
if name == 'free' and mode == 'timeout':
    time.sleep(5)
if name == 'df' and mode == 'stderr-warning':
    print('diagnostic warning', file=sys.stderr)
if name in ('df', 'free'):
    print(name + ' human readable output')
elif name == 'docker-compose':
    print('legacy compose status')
elif args[0] == 'stats':
    if mode == 'docker-down':
        print('Cannot connect to Docker', file=sys.stderr)
        sys.exit(1)
    print('CONTAINER CPU MEMORY')
elif args[:2] == ['ps', '-a']:
    if mode == 'docker-down':
        print('Cannot connect to Docker', file=sys.stderr)
        sys.exit(1)
    if '--format' in args:
        if mode == 'ambiguous':
            print('otherid\tother-pyrus-logs-pgsql\tother\tlogs')
        if mode == 'generated':
            print('webid\tmy-pyrus-web-api-1\tmy\tweb-api')
            print('logid\tmy-logs-1\tmy\tlogs')
        else:
            print('logid\tpyrus-logs-pgsql\tpyrus\tlogs')
    else:
        print('CONTAINER ID STATUS NAMES\nlogid Up pyrus-logs-pgsql')
elif args[0] == 'compose':
    if mode in ('no-compose', 'docker-down'):
        print('no configuration file provided', file=sys.stderr)
        sys.exit(1)
    print('NAME SERVICE STATUS')
elif args[0] == 'exec':
    sql = args[-1]
    if 'CURRENT_DATE' in sql:
        if mode == 'date-fail':
            print('database unavailable', file=sys.stderr)
            sys.exit(1)
        print('2025-07-10|UTC|2025-07-10 11:00:00+00')
    elif mode == 'missing' and 'warnings_' in sql:
        print('ERROR: relation does not exist', file=sys.stderr)
        sys.exit(1)
    elif mode == 'query-fail' and 'errors_' in sql:
        print('ERROR: connection lost', file=sys.stderr)
        sys.exit(2)
    elif mode == 'empty':
        print('(0 rows)')
    else:
        print('тестовая строка лога\n(1 row)')
else:
    print('Unexpected command', file=sys.stderr)
    sys.exit(99)
'''


class CollectLogsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.cwd = self.base / 'папка клиента с пробелами'
        self.cwd.mkdir()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.fake = self.bin / 'fake'
        self.fake.write_text(FAKE, encoding='utf-8')
        self.fake.chmod(0o755)
        for name in ('df', 'free', 'docker'):
            (self.bin / name).symlink_to(self.fake)
        self.calls = self.base / 'calls'
        self.env = dict(os.environ, PATH=str(self.bin) + ':' + os.environ['PATH'],
                        CALLS=str(self.calls), TMPDIR=str(self.tmp), SCENARIO='')
        self.env.pop('SUDO_UID', None)
        self.env.pop('SUDO_GID', None)

    def run_collector(self, scenario='', args=(), installer=False):
        previous_archives = set(self.cwd.glob('*.zip'))
        command = ['bash', str(ROOT / ('install.sh' if installer else 'collect-logs.sh'))]
        if installer:
            command.append('--logs')
        result = subprocess.run(command + list(args), cwd=self.cwd,
                                env=dict(self.env, SCENARIO=scenario),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        archives = list(self.cwd.glob('*.zip'))
        new_archives = set(archives) - previous_archives
        if new_archives:
            self.assertEqual(len(new_archives), 1)
            with zipfile.ZipFile(new_archives.pop()) as bundle:
                self.assertIsNone(bundle.testzip())
                files = {name: bundle.read(name).decode('utf-8') for name in bundle.namelist()}
        else:
            files = {}
        return result, files, archives

    def commands(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_success_current_database_day_and_read_only_queries(self):
        result, files, archives = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        for name in ('df.txt', 'free.txt', 'docker_stats.txt', 'docker_compose_ps.txt',
                     'errors.txt', 'warnings.txt', 'database_time.txt', 'report.txt'):
            self.assertIn(name, files)
        self.assertIn('2025-07-10', files['report.txt'])
        self.assertIn('СОБРАНО ПОЛНОСТЬЮ', files['report.txt'])
        self.assertIn('тестовая строка', files['errors.txt'])
        self.assertFalse(any(name.endswith('.stderr.txt') for name in files))
        commands = self.commands()
        queries = [c for c in commands if c[1] == 'exec']
        self.assertEqual(len(queries), 3)
        self.assertIn('SELECT * FROM events.errors_2025_07_10;', queries[1])
        self.assertIn('SELECT * FROM events.warnings_2025_07_10;', queries[2])
        for query in queries:
            self.assertNotIn('-it', query)
            self.assertIn('-X', query)
            self.assertIn('-w', query)
            self.assertIn('--set=ON_ERROR_STOP=1', query)
            self.assertIn('--set=FETCH_COUNT=1000', query)
            self.assertIn('default_transaction_read_only=on', ' '.join(query))
        self.assertFalse(any(c[1] in ('cp', 'run', 'restart', 'pull', 'up') for c in commands))
        self.assertEqual(list(self.tmp.iterdir()), [])
        self.assertEqual(list(self.cwd.iterdir()), archives)

    def test_explicit_date_and_container_from_installer(self):
        result, files, _ = self.run_collector(args=('--date', '2026-01-02', '--container', 'pyrus-logs-pgsql'), installer=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('events.errors_2026_01_02', files['report.txt'])

    def test_invalid_and_injected_dates_are_rejected_before_commands(self):
        for value in ('2025-02-30', '2025-1-1', '2025-07-10;DROP TABLE events.errors;', '$(touch bad)'):
            result, _, archives = self.run_collector(args=('--date', value))
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(archives, [])
        self.assertFalse(self.calls.exists())

    def test_missing_partition_and_query_failure_produce_partial_zip(self):
        for scenario in ('missing', 'query-fail'):
            result, files, archives = self.run_collector(scenario)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertTrue(archives)
            self.assertIn('СОБРАНО ЧАСТИЧНО', files['report.txt'])
            failed, succeeded = ('warnings', 'errors') if scenario == 'missing' else ('errors', 'warnings')
            self.assertIn('ERROR:', files[failed + '.stderr.txt'])
            self.assertNotIn(succeeded + '.stderr.txt', files)

    def test_nonempty_stderr_is_kept_even_when_command_succeeds(self):
        result, files, _ = self.run_collector('stderr-warning')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('diagnostic warning', files['df.stderr.txt'])
        self.assertNotIn('free.stderr.txt', files)

    def test_empty_tables_are_a_success(self):
        result, files, _ = self.run_collector('empty')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('(0 rows)', files['errors.txt'])

    def test_ambiguous_installations_skip_queries_and_report_selection(self):
        result, files, _ = self.run_collector('ambiguous')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('несколько БД логов', files['report.txt'])
        self.assertFalse(any(c[1] == 'exec' for c in self.commands()))
        result, _, _ = self.run_collector('ambiguous', args=('--project', 'pyrus'))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_compose_container_name_is_discovered(self):
        result, files, _ = self.run_collector('generated')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('my-logs-1', files['report.txt'])

    def test_compose_failure_keeps_output_and_falls_back_to_project_labels(self):
        result, files, _ = self.run_collector('no-compose')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('no configuration file', files['docker_compose_ps.stderr.txt'])
        self.assertIn('pyrus-logs-pgsql', files['docker_compose_ps_fallback.txt'])
        self.assertIn(['docker', 'ps', '-a', '--no-trunc', '--filter',
                       'label=com.docker.compose.project=pyrus'], self.commands())

    def test_legacy_compose_is_supported(self):
        (self.bin / 'docker-compose').symlink_to(self.fake)
        result, files, _ = self.run_collector()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('legacy compose', files['docker_compose_ps.txt'])

    def test_database_date_failure_does_not_silently_use_host_date(self):
        result, files, _ = self.run_collector('date-fail')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Не удалось определить дату БД', files['errors.stderr.txt'])
        self.assertEqual(len([c for c in self.commands() if c[1] == 'exec']), 1)

    def test_no_docker_still_archives_host_diagnostics(self):
        result, files, _ = self.run_collector('docker-down')
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('human readable', files['df.txt'])
        self.assertIn('human readable', files['free.txt'])
        self.assertIn('Cannot connect', files['docker_stats.stderr.txt'])
        self.assertIn('не найден', files['errors.stderr.txt'])

    def test_timeout_preserves_zip_and_cleans_temporary_files(self):
        result, files, _ = self.run_collector('timeout', args=('--timeout', '1'))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('Таймаут', files['free.stderr.txt'])
        self.assertIn('[ERROR 124] free', files['report.txt'])
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_repeat_runs_do_not_overwrite_archives(self):
        self.assertEqual(self.run_collector()[0].returncode, 0)
        self.assertEqual(self.run_collector()[0].returncode, 0)
        self.assertEqual(len(list(self.cwd.glob('*.zip'))), 2)


if __name__ == '__main__':
    unittest.main()
