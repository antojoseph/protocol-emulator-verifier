"""Run candidate compilers without network, user files, or judge write access."""
from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import shutil
import signal
import secrets
import subprocess
import sys
import tempfile

from .common import ROOT, ToolUnavailable, VerificationError


def compiler(candidate, manifest, workdir):
    candidate = Path(candidate).resolve()
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    count = 0

    def invoke(request):
        nonlocal count
        count += 1
        invocation = workdir / ('request-%04d' % count)
        invocation.mkdir()
        docker = shutil.which('docker')
        if not docker:
            raise ToolUnavailable('Docker is required for isolated candidate compilation; start Docker and run ./setup.sh')
        lock = json.loads((ROOT / 'verifier/physical_support/flow-lock.json').read_text())
        container_name = 'protocol-compiler-' + secrets.token_hex(12)
        command = [docker, 'run', '-i', '--rm', '--name', container_name, '--pull=never', '--platform=linux/amd64',
                   '--network=none', '--read-only', '--cap-drop=ALL',
                   '--security-opt=no-new-privileges', '--pids-limit=64',
                   '--memory=512m', '--cpus=1', '--user=65534:65534',
                   '--ulimit=cpu=15:15', '--ulimit=nofile=64:64', '--ulimit=fsize=4000000:4000000',
                   '--tmpfs=/tmp:rw,nosuid,noexec,size=64m',
                   '--mount', 'type=bind,src=' + str(candidate) + ',dst=/candidate,readonly',
                   '--workdir=/tmp', '--entrypoint=python3', lock['container'],
                   '-I', '-B', '/candidate/' + manifest['adapter']]
        environment = {'PATH': '/usr/bin:/bin', 'HOME': str(invocation),
                       'TMPDIR': str(invocation), 'LC_ALL': 'C', 'PYTHONDONTWRITEBYTECODE': '1'}

        def limits():
            os.setsid()
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
            resource.setrlimit(resource.RLIMIT_FSIZE, (4_000_000, 4_000_000))
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(command, cwd=invocation, env=environment,
                                       stdin=subprocess.PIPE, stdout=out, stderr=err, preexec_fn=limits)
            try:
                process.communicate(json.dumps(request).encode(), timeout=25)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                raise VerificationError('Candidate compiler timed out')
            finally:
                try:
                    subprocess.run([docker, 'rm', '--force', container_name], env=environment,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
                except subprocess.TimeoutExpired:
                    raise VerificationError('Could not confirm candidate container cleanup')
            out.seek(0); err.seek(0)
            stdout, stderr = out.read(4_000_001), err.read(4_000_001)
        if process.returncode != 0:
            raise VerificationError('Isolated candidate compiler failed (exit %s): ' % process.returncode + stderr.decode(errors='replace')[:1000])
        if len(stdout) > 4_000_000 or len(stderr) > 4_000_000:
            raise VerificationError('Candidate compiler output limit exceeded')
        def invalid(value):
            raise VerificationError('Non-finite compiler output')
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise VerificationError('Duplicate compiler JSON key')
                result[key] = value
            return result
        try:
            result = json.loads(stdout, parse_constant=invalid, object_pairs_hook=pairs)
        except (ValueError, UnicodeError) as error:
            raise VerificationError('Candidate compiler did not return valid JSON') from error
        if not isinstance(result, dict):
            raise VerificationError('Candidate compiler must return a JSON object')
        return result
    return invoke
