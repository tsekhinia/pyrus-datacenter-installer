#!/usr/bin/env python3
"""Manage an existing Pyrus deployment using the local Docker API via its CLI.

Works with containers created by either docker-compose or docker compose.
Never renders/replaces their Compose files, starts dependencies, or pulls images.
Only non-secret container IDs and image/network metadata are saved temporarily.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import config
from compose import RUNTIME

INFRASTRUCTURE = {'postgres', 'logs', 'elastic', 'nats', 'minio'}
APPLICATION = set(RUNTIME) - INFRASTRUCTURE
CONNECTIONS = ('MAIN_CONN_STRING', 'PRIVATE_LOGS_CONN_STRING', 'PUBLIC_LOGS_CONN_STRING')


def docker(*args):
    try:
        result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                universal_newlines=True, timeout=30, check=True)
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise ValueError('Не удалось прочитать метаданные Docker. Проверьте docker ps -a.') from None


def inspect(*identifiers):
    return json.loads(docker('inspect', *identifiers))


def labels(container):
    return container.get('Config', {}).get('Labels') or {}


def service(container):
    return labels(container).get('com.docker.compose.service', '')


def running(container):
    return container.get('State', {}).get('Running', False)


def discover(project_filter=''):
    ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project').split()
    projects = {}
    for offset in range(0, len(ids), 100):
        for container in inspect(*ids[offset:offset + 100]):
            meta = labels(container)
            project = meta.get('com.docker.compose.project', '')
            if not project or (project_filter and project != project_filter):
                continue
            if str(meta.get('com.docker.compose.oneoff', '')).lower() == 'true':
                continue
            projects.setdefault(project, []).append(container)
    result = []
    for project, containers in sorted(projects.items()):
        anchors = [c for c in containers if service(c) == 'web-api' and
                   re.search(r'(^|/)(?:dev-)?pyrus-web-api(?=[:@]|$)', c['Config'].get('Image', ''))]
        if not anchors:
            continue
        # Do not persist Config.Env; it contains the live database credentials.
        result.append({'project': project, 'anchors': [c['Id'] for c in anchors],
                       'containers': [c['Id'] for c in containers]})
    return result


def image(identifier):
    try:
        return json.loads(docker('image', 'inspect', identifier))[0]
    except (ValueError, IndexError):
        raise ValueError('Локальный образ pyrus-setup не найден. Укажите --menu --setup-image IMAGE той же версии, что и Pyrus.') from None


def setup_image(anchor, explicit):
    reference = anchor['Config']['Image']
    references = [reference]
    if '@' in reference or ':' not in reference.rsplit('/', 1)[-1]:
        references = image(anchor['Image']).get('RepoTags') or []
    candidates = set()
    expected_variants = set()
    for reference in references:
        match = re.fullmatch(r'(.*/)?(dev-)?pyrus-web-api:([0-9]+\.[0-9]+\.[0-9]+)', reference)
        if match:
            prefix, variant, version = match.groups()
            variant = variant or ''
            candidates.add((prefix or '') + variant + 'pyrus-setup:' + version)
            expected_variants.add((variant, version))
    if explicit:
        selected = image(explicit)
        actual_variants = set()
        for ref in [explicit, *(selected.get('RepoTags') or [])]:
            match = re.search(r'(?:^|/)(dev-)?pyrus-setup:([0-9]+\.[0-9]+\.[0-9]+)$', ref)
            if match:
                actual_variants.add((match[1] or '', match[2]))
        if expected_variants and not expected_variants.intersection(actual_variants):
            raise ValueError('Вариант или версия локального pyrus-setup не совпадает с версией/вариантом работающего Pyrus')
        return selected['Id'], explicit
    if len(candidates) != 1:
        raise ValueError('Не удалось однозначно выбрать версию pyrus-setup. Укажите --menu --setup-image IMAGE из локальных образов.')
    reference = candidates.pop()
    return image(reference)['Id'], reference


def network_id(anchor, database):
    def networks(container):
        return container.get('NetworkSettings', {}).get('Networks') or {}
    first, second = networks(anchor), networks(database)
    common = {first[name].get('NetworkID') for name in first.keys() & second.keys()
              if first[name].get('NetworkID') and first[name].get('NetworkID') == second[name].get('NetworkID')}
    if len(common) != 1:
        raise ValueError('Нужна одна общая Docker-сеть web-api и postgres; неоднозначное подключение остановлено.')
    return common.pop()


def certificate(containers, explicit):
    if explicit:
        path = Path(explicit)
        if not path.is_absolute() or not path.is_file():
            raise ValueError('--tls-cert должен указывать на существующий PEM-файл по абсолютному пути')
        return str(path)
    paths = set()
    for container in containers:
        if service(container) != 'nginx':
            continue
        for mount in container.get('Mounts', []):
            if mount.get('Type') != 'bind':
                continue
            source = Path(mount['Source'])
            target = mount['Destination']
            if target == '/etc/nginx/ssl/cert.crt' and source.is_file():
                paths.add(str(source))
            elif target == '/etc/nginx/ssl' and (source / 'cert.crt').is_file():
                paths.add(str(source / 'cert.crt'))
    return paths.pop() if len(paths) == 1 else ''


def select(project, explicit_image='', explicit_cert=''):
    containers = inspect(*project['containers'])
    anchors = [c for c in containers if c['Id'] in project['anchors'] and running(c)]
    databases = [c for c in containers if service(c) == 'postgres' and running(c)]
    if len(anchors) != 1 or len(databases) != 1:
        raise ValueError('Нужны работающие web-api и postgres выбранной установки. Меню не запускает остановленные сервисы.')
    anchor, database = anchors[0], databases[0]
    if env(anchor).get('MAIN_CONN_STRING', '') == '':
        raise ValueError('У web-api нет MAIN_CONN_STRING. Этот вариант установки пока не поддерживается.')
    identifier, reference = setup_image(anchor, explicit_image)
    return {'project': project['project'], 'anchor': anchor['Id'], 'database': database['Id'],
            'anchor_image': anchor['Image'], 'database_image': database['Image'],
            'network': network_id(anchor, database), 'setup_image': identifier, 'setup_reference': reference,
            'certificate': certificate(containers, explicit_cert),
            'applications': [c['Id'] for c in containers if service(c) in APPLICATION and running(c)]}


def env(container):
    return dict(value.split('=', 1) for value in container.get('Config', {}).get('Env', []) if '=' in value)


def verify(target):
    anchor, database = inspect(target['anchor'], target['database'])
    if (anchor['Image'] != target['anchor_image'] or database['Image'] != target['database_image'] or
            not running(anchor) or not running(database) or network_id(anchor, database) != target['network']):
        raise ValueError('Контейнеры или сеть изменились. Откройте меню заново.')
    if any(labels(c).get('com.docker.compose.project') != target['project'] for c in [anchor, database]):
        raise ValueError('Проект Docker изменился. Откройте меню заново.')
    return anchor


def run_setup(target, args):
    # This adapter exposes only the documented list and set operations.
    if not (args == ['--setup', '-ls'] or
            (len(args) == 4 and args[:3] == ['--setup', '-ls', '-id'] and re.fullmatch(r'[1-9][0-9]*', args[3])) or
            (len(args) == 5 and args[:2] == ['--setup', '-id'] and re.fullmatch(r'[1-9][0-9]*', args[2]) and args[3] == '-v')):
        raise ValueError('Для существующей установки разрешены только чтение и запись параметров')
    source = env(verify(target))
    if not source.get('MAIN_CONN_STRING'):
        raise ValueError('MAIN_CONN_STRING отсутствует. Откройте меню заново.')
    image(target['setup_image'])  # A removed image must not trigger a pull.
    command = ['docker', 'run', '--rm', '--network', target['network']]
    child_env = os.environ.copy()
    child_env['APPLICATION_NAME'] = 'pyrus-setup'
    command += ['--env', 'APPLICATION_NAME']
    for key in CONNECTIONS:
        if key in source:
            child_env[key] = source[key]
            command += ['--env', key]  # Values inherited via env, never argv/files.
        else:
            child_env.pop(key, None)
    command += [target['setup_image'], *args]
    return subprocess.call(command, env=child_env, timeout=1800)


def restart(target):
    verify(target)
    identifiers = target['applications']
    if not identifiers:
        raise ValueError('Нет запущенных сервисов приложения для перезапуска')
    for container in inspect(*identifiers):
        if (labels(container).get('com.docker.compose.project') != target['project'] or
                service(container) not in APPLICATION or not running(container)):
            raise ValueError('Набор сервисов изменился. Откройте меню заново.')
    return subprocess.call(['docker', 'restart', *identifiers], timeout=1800)


def lock_path(target, directory):
    directory = Path(directory)
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    config.private(directory)
    digest = hashlib.sha256((target['project'] + '\0' + target['database']).encode()).hexdigest()
    path = directory / (digest + '.lock')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    config.private(path)
    return str(path)


def main():
    action, path, *args = sys.argv[1:]
    if action == 'discover':
        config.write(path, {'projects': discover(args[0])})
        return
    data = json.loads(Path(path).read_text())
    if action == 'count':
        print(len(data['projects']))
    elif action == 'list':
        for i, project in enumerate(data['projects'], 1):
            print(str(i) + '. Pyrus: ' + json.dumps(project['project'], ensure_ascii=False))
    elif action == 'select':
        if not re.fullmatch(r'[1-9][0-9]*', args[0]) or not 1 <= int(args[0]) <= len(data['projects']):
            raise ValueError('Выберите номер установки из списка')
        target = select(data['projects'][int(args[0]) - 1], args[2], args[3])
        config.write(args[1], target)
        print('Pyrus: ' + json.dumps(target['project']) + '; локальный образ: ' + json.dumps(target['setup_reference']))
    elif action == 'field':
        print(data[args[0]])
    elif action == 'lock':
        print(lock_path(data, args[0]))
    elif action == 'setup':
        sys.exit(run_setup(data, args))
    elif action == 'restart':
        sys.exit(restart(data))
    else:
        raise ValueError('Неизвестная операция')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        print('Существующая установка: ' + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, KeyError, IndexError, subprocess.SubprocessError):
        # Never print raw Docker output, Env, or subprocess exception arguments.
        print('Не удалось подключиться к существующему Pyrus. Проверьте работающие web-api/postgres, локальный образ pyrus-setup той же версии и выбранный проект.', file=sys.stderr)
        sys.exit(1)
