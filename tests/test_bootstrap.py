"""Exercise the real bootstrap offline with a local bundle and fake downloads."""
import hashlib
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMMIT = next(line.split('=', 1)[1] for line in (ROOT / 'bootstrap.sh').read_text().splitlines()
              if line.startswith('readonly INSTALLER_COMMIT='))


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.bin = self.base / 'bin'
        self.bin.mkdir()
        self.tmp = self.base / 'tmp'
        self.tmp.mkdir()
        self.calls = self.base / 'calls.jsonl'
        self.result = self.base / 'result.json'
        self.archive = self.base / 'bundle.tar.gz'
        installer = '''#!/usr/bin/env bash
set -euo pipefail
if [[ ${MOCK_WIZARD:-0} == 1 ]]; then
    [[ -t 0 ]] || exit 91
    printf 'Wizard ready: '
    read -r answer
    [[ "$answer" == yes ]] || exit 92
fi
python3 - "$@" <<'PY'
import json, os, sys
from pathlib import Path
Path(os.environ['MOCK_RESULT']).write_text(json.dumps(sys.argv[1:]))
PY
exit "${MOCK_INSTALLER_STATUS:-0}"
'''
        with tarfile.open(self.archive, 'w:gz') as archive:
            for name, content in [('install.sh', installer), ('lib/common.sh', '# module'),
                                  ('scripts/config.py', '# helper')]:
                data = content.encode()
                member = tarfile.TarInfo(f'pyrus-datacenter-installer-{COMMIT}/{name}')
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        source = (ROOT / 'bootstrap.sh').read_text()
        self.bootstrap = self.base / 'bootstrap.sh'
        # Only replace the trusted bundle checksum; run the production control flow.
        source = source.replace(next(line for line in source.splitlines()
                                     if line.startswith('readonly ARCHIVE_SHA256=')),
                                f'readonly ARCHIVE_SHA256={digest}')
        self.bootstrap.write_text(source)
        fake = self.bin / 'download'
        fake.write_text(f'#!{sys.executable}\n' + '''import json, os, shutil, sys
from pathlib import Path
with open(os.environ['MOCK_CALLS'], 'a') as f:
    f.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')
if os.environ.get('MOCK_DOWNLOAD_FAIL'):
    sys.exit(22)
args = sys.argv[1:]
if '--output' in args:
    dest = args[args.index('--output') + 1]
else:
    dest = next(x.split('=', 1)[1] for x in args if x.startswith('--output-document='))
shutil.copyfile(os.environ['MOCK_ARCHIVE'], dest)
if os.environ.get('MOCK_CORRUPT'):
    with open(dest, 'ab') as f:
        f.write(b'corrupt')
''')
        fake.chmod(0o755)
        for name in ['curl', 'wget']:
            (self.bin / name).symlink_to(fake)
        # An isolated PATH also lets the wget-only case prove no curl is needed.
        for name in ['bash', 'tar', 'gzip', 'mktemp', 'rm']:
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / 'python3').symlink_to(sys.executable)
        sha = self.bin / 'sha256sum'
        sha.write_text(f'#!{sys.executable}\n' + '''import hashlib, sys
from pathlib import Path
digest, path = sys.stdin.readline().rstrip('\\n').split('  ', 1)
sys.exit(0 if hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest else 1)
''')
        sha.chmod(0o755)
        self.env = os.environ | dict(PATH=str(self.bin), TMPDIR=str(self.tmp),
                                     MOCK_ARCHIVE=str(self.archive), MOCK_CALLS=str(self.calls),
                                     MOCK_RESULT=str(self.result))

    def run_bootstrap(self, *args, extra_env=None):
        return subprocess.run(['bash', str(self.bootstrap), *args], env=self.env | (extra_env or {}),
                              text=True, capture_output=True, timeout=15)

    def assert_clean(self):
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_curl_arguments_and_cleanup(self):
        args = ['--resume', '--install-dir', '/srv/pyrus', '--config', '/root/a b.json']
        result = self.run_bootstrap(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.result.read_text()), args)
        call = json.loads(self.calls.read_text())
        self.assertEqual(call[0], 'curl')
        self.assertIn(f'https://codeload.github.com/tsekhinia/pyrus-datacenter-installer/tar.gz/{COMMIT}', call)
        self.assertIn('--fail', call)
        self.assertIn('--proto-redir', call)
        self.assert_clean()

    def test_wget_without_curl(self):
        (self.bin / 'curl').unlink()
        result = self.run_bootstrap('--preflight')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(self.calls.read_text())[0], 'wget')
        self.assertEqual(json.loads(self.result.read_text()), ['--preflight'])
        self.assert_clean()

    def test_http_failure_never_executes(self):
        for downloader in ['curl', 'wget']:
            with self.subTest(downloader=downloader):
                if downloader == 'wget':
                    (self.bin / 'curl').unlink()
                result = self.run_bootstrap(extra_env={'MOCK_DOWNLOAD_FAIL': '1'})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('download failed', result.stderr)
                self.assertFalse(self.result.exists())
                self.assert_clean()

    def test_checksum_failure_never_executes(self):
        result = self.run_bootstrap(extra_env={'MOCK_CORRUPT': '1'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertFalse(self.result.exists())
        self.assert_clean()

    def test_child_exit_status_and_cleanup(self):
        result = self.run_bootstrap(extra_env={'MOCK_INSTALLER_STATUS': '37'})
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assert_clean()

    def test_pipe_reconnects_wizard_to_terminal(self):
        pid, terminal = pty.fork()
        if pid == 0:
            # Read the bootstrap through a pipe, as wget -qO- URL | bash -s -- ... does.
            command = f'cat {shlex.quote(str(self.bootstrap))} | bash -s -- --accept-low-resources'
            (self.bin / 'cat').symlink_to(shutil.which('cat'))
            os.execve(str(self.bin / 'bash'), ['bash', '-c', command],
                      self.env | {'MOCK_WIZARD': '1'})
        output = b''
        status = None
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([terminal], [], [], 0.1)[0]:
                    try:
                        output += os.read(terminal, 65536)
                    except OSError:
                        break
                    if b'Wizard ready:' in output and b'yes\r\n' not in output:
                        os.write(terminal, b'yes\n')
                child, status = os.waitpid(pid, os.WNOHANG)
                if child:
                    break
            else:
                self.fail(f'Wizard timed out: {output!r}')
        finally:
            os.close(terminal)
            # Reap the process if the terminal closed before waitpid observed exit.
            if status is None or status == 0:
                try:
                    _, status = os.waitpid(pid, 0)
                except ChildProcessError:
                    pass
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors='replace'))
        self.assertEqual(json.loads(self.result.read_text()), ['--accept-low-resources'])
        self.assert_clean()
