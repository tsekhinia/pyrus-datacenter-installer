#!/usr/bin/env python3
"""Filter known secrets out of command output; argv is never logged."""
import json
from pathlib import Path
import sys

try:
    secrets = json.loads(Path(sys.argv[1]).read_text())
    values = sorted({v for v in secrets.values() if v}, key=len, reverse=True)
    for line in sys.stdin:
        for secret in values:
            line = line.replace(secret, '[REDACTED]')
        sys.stdout.write(line)
except (OSError, ValueError):
    # Never copy unfiltered output to a log if loading the secrets fails.
    print('Secret redaction failed; command output was discarded.', file=sys.stderr)
    sys.exit(1)
