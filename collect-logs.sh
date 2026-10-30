#!/usr/bin/env bash
# Standalone: no installer bundle, Compose file or setup image required.
set +x
set -euo pipefail
command -v python3 >/dev/null || { printf 'Требуется python3.\n' >&2; exit 1; }
exec python3 - "$@" <<'PYTHON'
import argparse
import datetime as dt
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import zipfile


def parse_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise argparse.ArgumentTypeError('Дата должна иметь вид YYYY-MM-DD')
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def positive_seconds(value):
    try:
        number = int(value)
        if 1 <= number <= 86400:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('Таймаут: целое число от 1 до 86400 секунд')


class Collector:
    def __init__(self, directory, timeout):
        self.directory = directory
        self.timeout = timeout
        self.results = []
        self.notes = []

    def note(self, message):
        self.notes.append(message)

    def run(self, name, command):
        # Stream stdout/stderr to disk: daily log tables can exceed host RAM.
        output = self.directory / (name + '.txt')
        error = self.directory / (name + '.stderr.txt')
        code = 0
        with output.open('wb') as stdout, error.open('wb') as stderr:
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr)
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    stderr.write(('\nТаймаут: %s сек.\n' % self.timeout).encode())
                    code = 124
            except OSError as exc:
                stderr.write((str(exc) + '\n').encode())
                code = 127
        self.results.append((name, code, shlex.join(command)))
        print('[%s] %s' % ('OK' if code == 0 else 'ОШИБКА %s' % code, name), flush=True)
        return code, output

    def skip(self, name, reason):
        (self.directory / (name + '.txt')).write_text('Не выгружено.\n', encoding='utf-8')
        (self.directory / (name + '.stderr.txt')).write_text(reason + '\n', encoding='utf-8')
        self.results.append((name, 1, reason))

    def psql(self, container, sql, tuples=False):
        # No TTY, shell, .psqlrc, password prompt or files inside the container.
        options = '-c default_transaction_read_only=on -c statement_timeout=%s -c lock_timeout=5000' % (self.timeout * 1000)
        command = ['docker', 'exec', '-e', 'PGOPTIONS=' + options, container,
                   'psql', '-X', '-w', '-U', 'pyrusadm', '-d', 'pyrus_logs',
                   '--set=ON_ERROR_STOP=1', '--set=FETCH_COUNT=1000', '-P', 'pager=off']
        if tuples:
            command += ['-A', '-t']
        return command + ['-c', sql]


def discover(rows, args):
    containers = []
    for line in rows.splitlines():
        fields = line.split('\t')
        if len(fields) == 4:
            identifier, name, project, service = fields
            containers.append(dict(id=identifier, name=name, project=project, service=service))
    if args.container:
        matches = [c for c in containers if args.container in (c['id'], c['name'])]
    else:
        # Recognize both the documented fixed name and Compose-generated names.
        pyrus_projects = {c['project'] for c in containers
                          if 'pyrus' in c['name'].lower() and c['project']}
        matches = [c for c in containers if c['name'] == 'pyrus-logs-pgsql'
                   or 'pyrus-logs-pgsql' in c['name'].lower()
                   or (c['service'] == 'logs' and c['project'] in pyrus_projects)]
    if args.project:
        matches = [c for c in matches if c['project'] == args.project]
    if len(matches) == 1:
        return matches[0], ''
    if not matches:
        return None, 'Контейнер БД логов не найден. Укажите --container ИМЯ (и при необходимости --project ИМЯ).'
    return None, 'Найдено несколько БД логов: %s. Укажите --container ИМЯ или --project ИМЯ.' % ', '.join(c['name'] for c in matches)


def compose_status(collector, project):
    options = ['-p', project] if project else []
    # Capture the requested command, with compatibility for Docker Compose V1.
    command = (['docker', 'compose'] if shutil.which('docker-compose') is None
               else ['docker-compose']) + options + ['ps', '-a']
    code, _ = collector.run('docker_compose_ps', command)
    if code:
        # An arbitrary launch directory may have no Compose YAML/.env. Docker
        # labels provide the same project inventory without loading private config.
        filters = ['--filter', 'label=com.docker.compose.project=' + project] if project else []
        collector.run('docker_compose_ps_fallback', ['docker', 'ps', '-a', '--no-trunc'] + filters)
        collector.note('Compose ps не выполнен; docker_compose_ps_fallback.txt содержит список контейнеров%s из Docker%s.' %
                       (' проекта ' + project if project else '', ' (все проекты)' if not project else ''))


def main():
    parser = argparse.ArgumentParser(prog='collect-logs.sh', description='Собрать диагностику Pyrus в ZIP в текущей папке.')
    parser.add_argument('--date', type=parse_date, help='Дата логов YYYY-MM-DD; по умолчанию сегодня по часам PostgreSQL')
    parser.add_argument('--container', help='Контейнер PostgreSQL логов (обычно pyrus-logs-pgsql)')
    parser.add_argument('--project', help='Compose-проект, если на сервере несколько установок')
    parser.add_argument('--timeout', type=positive_seconds, default=180, help='Таймаут каждой команды в секундах (180)')
    args = parser.parse_args()
    os.umask(0o077)
    destination = Path.cwd()
    started = dt.datetime.now().astimezone()
    host = re.sub(r'[^A-Za-z0-9_.-]', '_', socket.gethostname())[:80] or 'host'
    archive = destination / ('pyrus-diagnostics-%s-%s-%s.zip' %
                             (host, started.strftime('%Y%m%d-%H%M%S'), uuid.uuid4().hex[:8]))
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix='pyrus-diagnostics-') as temporary:
            collector = Collector(Path(temporary), args.timeout)
            collector.run('df', ['df', '-h'])
            collector.run('free', ['free', '-h'])
            collector.run('docker_stats', ['docker', 'stats', '--no-stream'])
            code, inventory = collector.run('docker_containers', ['docker', 'ps', '-a', '--format',
                '{{.ID}}\t{{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'])
            target, problem = discover(inventory.read_text(encoding='utf-8', errors='replace'), args)
            compose_status(collector, args.project or (target['project'] if target else ''))
            day = args.date
            if target:
                collector.note('Контейнер логов: %s; Compose-проект: %s.' % (target['name'], target['project'] or '(нет метки)'))
                code, metadata = collector.run('database_time', collector.psql(target['id'],
                    "SELECT CURRENT_DATE, current_setting('TimeZone'), now();", tuples=True))
                if day is None and code == 0:
                    try:
                        day = parse_date(metadata.read_text(encoding='utf-8').strip().split('|')[0])
                    except (ValueError, argparse.ArgumentTypeError):
                        collector.note('Не удалось определить текущую дату PostgreSQL; дата хоста не подставляется.')
                for kind in ('errors', 'warnings'):
                    if day is None:
                        collector.skip(kind, 'Не удалось определить дату БД. См. database_time.stderr.txt или задайте --date YYYY-MM-DD.')
                    else:
                        table = 'events.%s_%s' % (kind, day.strftime('%Y_%m_%d'))
                        collector.run(kind, collector.psql(target['id'], 'SELECT * FROM %s;' % table))
                if day:
                    collector.note('Дата таблиц логов: %s. Дата по умолчанию берётся из PostgreSQL.' % day.isoformat())
            else:
                collector.note(problem)
                for kind in ('errors', 'warnings'):
                    collector.skip(kind, problem)
            incomplete = any(code != 0 for _, code, _ in collector.results)
            report = ['Диагностика Pyrus Datacenter', 'Начало: ' + started.isoformat(),
                      'Завершение: ' + dt.datetime.now().astimezone().isoformat(),
                      'Папка запуска: ' + str(destination),
                      'Результат: ' + ('СОБРАНО ЧАСТИЧНО' if incomplete else 'СОБРАНО ПОЛНОСТЬЮ'), '',
                      'Каждая команда: *.txt — stdout, *.stderr.txt — stderr.',
                      'Ошибка отсутствующей таблицы не означает отсутствие событий.',
                      'Архив содержит исходные логи; в них могут быть конфиденциальные данные.', '']
            report += collector.notes + ['']
            report += ['[%s] %s\n  %s' % ('OK' if code == 0 else 'ERROR %s' % code, name, command)
                       for name, code, command in collector.results]
            (Path(temporary) / 'report.txt').write_text('\n'.join(report) + '\n', encoding='utf-8')
            # Exclusive creation avoids overwriting an older report or a symlink.
            with archive.open('xb') as output:
                created = True
                with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, allowZip64=True) as bundle:
                    for path in sorted(Path(temporary).iterdir()):
                        bundle.write(path, arcname=path.name)
                # Change ownership through the open descriptor, even in a
                # user-writable launch directory (do not follow a swapped path).
                if os.geteuid() == 0 and os.environ.get('SUDO_UID', '').isdigit() and os.environ.get('SUDO_GID', '').isdigit():
                    try:
                        os.fchown(output.fileno(), int(os.environ['SUDO_UID']), int(os.environ['SUDO_GID']))
                    except OSError as exc:
                        print('ZIP создан, но не удалось сменить владельца: %s' % exc, file=sys.stderr)
            print('\nZIP: %s' % archive, flush=True)
            if incomplete:
                print('Собрано частично. Причины: report.txt и *.stderr.txt внутри архива.', file=sys.stderr)
                return 2
            return 0
    except (OSError, zipfile.BadZipFile, RuntimeError, KeyboardInterrupt) as exc:
        if created:
            archive.unlink(missing_ok=True)
        print('Не удалось создать ZIP: %s' % (exc or 'сбор прерван'), file=sys.stderr)
        return 1


sys.exit(main())
PYTHON
