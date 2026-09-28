#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "${1:-}" in
    ""|--physical) ;;
    *) echo "Usage: ./setup.sh [--physical]" >&2; exit 2 ;;
esac
python3 - <<'PY'
import json, subprocess
import sys
from pathlib import Path
if sys.version_info < (3, 9):
    raise SystemExit('Python 3.9 or newer is required.')
lock=json.loads(Path('verifier/physical_support/flow-lock.json').read_text())
subprocess.run(['docker','info','--format','{{.ServerVersion}}'],check=True,timeout=20)
subprocess.run(['docker','pull','--platform=linux/amd64',lock['container']],check=True)
PY
if [ "${1:-}" = "--physical" ]; then
    python3 scripts/setup_physical.py --skip-image
fi
