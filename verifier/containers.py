"""Bounded execution of untrusted HDL in the pinned, networkless EDA image."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import time

from .common import ROOT, ToolUnavailable


def run_eda(argv, *, cwd, readonly, log_path, timeout=180, memory='8g', max_output=8_000_000):
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    log_path = Path(log_path)
    docker = shutil.which('docker')
    if not docker:
        raise ToolUnavailable('Docker is required to isolate candidate HDL')
    lock = json.loads((ROOT / 'verifier/physical_support/flow-lock.json').read_text())
    name = 'protocol-eda-' + secrets.token_hex(12)
    executable = Path(argv[0]).name
    if executable not in ('yosys', 'iverilog', 'vvp'):
        raise ValueError('Unsupported trusted EDA tool')
    command = [docker, 'run', '--rm', '--name', name, '--pull=never',
               '--platform=linux/amd64', '--network=none', '--read-only',
               '--cap-drop=ALL', '--security-opt=no-new-privileges', '--pids-limit=256',
               '--memory=' + memory, '--cpus=2', '--ulimit=nofile=1024:1024',
               '--ulimit=fsize=536870912:536870912',
               '--tmpfs=/tmp:rw,nosuid,size=256m', '--user', f'{os.getuid()}:{os.getgid()}',
               '--workdir', str(cwd), '--env', 'HOME=/tmp']
    mounts = sorted(set(str(Path(p).resolve()) for p in readonly))
    # Parent read-only mounts precede the writable scratch-directory mount.
    for path in mounts:
        if path == str(cwd) or Path(path).is_relative_to(cwd):
            continue
        command += ['--mount', f'type=bind,src={path},dst={path},readonly']
    command += ['--mount', f'type=bind,src={cwd},dst={cwd}',
                '--entrypoint=' + executable, lock['container'], *map(str, argv[1:])]
    started = time.monotonic()
    process = None
    detail = ''
    ok = False
    try:
        with log_path.open('w') as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            while process.poll() is None:
                if time.monotonic()-started > timeout or log_path.stat().st_size > max_output:
                    detail = 'EDA timeout or output limit exceeded'
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
                    break
                time.sleep(.1)
            else:
                ok = process.returncode == 0 and log_path.stat().st_size <= max_output
                detail = 'exit %s' % process.returncode
    except OSError as error:
        detail = str(error)
    finally:
        try:
            subprocess.run([docker, 'rm', '--force', name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.TimeoutExpired):
            ok, detail = False, 'Could not confirm EDA container cleanup'
    return ok, detail
