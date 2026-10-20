#!/usr/bin/env python3
"""Read-only port and host collision checks."""
import socket
import subprocess
import sys

def check_ports():
    for port in (80, 443):
        for family, address in [(socket.AF_INET, '0.0.0.0'), (socket.AF_INET6, '::')]:
            try:
                with socket.socket(family) as s:
                    if family == socket.AF_INET6: s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    s.bind((address, port))
            except OSError as e:
                if e.errno in (97, 99): continue  # IPv6 unavailable
                raise ValueError(f'TCP port {port} is occupied or unavailable') from e
def check_resources():
    for kind in ('container', 'volume', 'network'):
        fmt = '{{.Names}}' if kind == 'container' else '{{.Name}}'
        command = ['docker', kind, 'ls'] + (['-a'] if kind == 'container' else []) + ['--format', fmt]
        names = subprocess.check_output(command, text=True, timeout=20).splitlines()
        if any(n.startswith(('pyrus-', 'pyrus_')) or n == 'pyrus' for n in names):
            raise ValueError(f'Existing Pyrus {kind} resources found. Installer supports fresh servers only.')

def main():
    if sys.argv[1] == 'connect':
        with socket.create_connection((sys.argv[2], int(sys.argv[3])), timeout=8): pass
    else:
        check_ports()
        check_resources()

if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        print('Preflight error: ' + str(e), file=sys.stderr); sys.exit(1)
