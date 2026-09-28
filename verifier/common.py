from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


class VerificationError(Exception):
    pass


class ToolUnavailable(VerificationError):
    pass


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    def invalid(value):
        raise VerificationError('Non-finite JSON value: ' + value)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise VerificationError('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), parse_constant=invalid, object_pairs_hook=pairs)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2, allow_nan=False) + '\n')
    temporary.replace(path)


def within(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise VerificationError('Invalid relative file path')
    path = Path(name)
    if path.is_absolute() or '..' in path.parts or any(p.startswith('.') for p in path.parts):
        raise VerificationError('Unsafe candidate path: ' + name)
    root = Path(root).resolve()
    target = root / path
    for parent in [target, *target.parents]:
        if parent == root:
            break
        if parent.is_symlink():
            raise VerificationError('Candidate symlink forbidden: ' + name)
    if not target.is_file() or not target.resolve().is_relative_to(root):
        raise VerificationError('Missing candidate file: ' + name)
    return target


def tool(name):
    local = ROOT / '.tools/oss-cad-suite/bin' / name
    result = local if local.exists() else shutil.which(name)
    if not result:
        raise ToolUnavailable('Missing ' + name + '; run ./setup.sh')
    return str(result)


def run(command, *, cwd, timeout=120, env=None, max_output=8_000_000):
    """Bounded trusted-tool execution. Logs go to a temporary file, not RAM."""
    import signal
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise VerificationError('Timed out: ' + str(command[0]))
        size = log.tell()
        log.seek(0)
        text = log.read(max_output).decode('utf-8', errors='replace')
        if size > max_output:
            raise VerificationError('Tool output exceeded limit: ' + str(command[0]))
    return status, text


def finite_number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise VerificationError('Invalid numeric metric: ' + name)
    if positive and value <= 0:
        raise VerificationError('Nonpositive metric: ' + name)
    return value
