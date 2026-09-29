"""Bounded scheduling and sealed inputs for the trusted physical pipeline."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time


class ExecutionError(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


class PhysicalRunner:
    """Own every subprocess/container; one absolute deadline covers both branches."""
    def __init__(self, work, deadline, budget_error, max_log_bytes):
        self.work = Path(work)
        self.deadline = deadline
        self.budget_error = budget_error
        self.max_log_bytes = max_log_bytes
        self.commands = []
        self.containers = []
        self._lock = threading.RLock()
        self._processes = set()
        self._signalled = set()
        self._cancelled = threading.Event()
        self._last_scan = 0.0

    def register_container(self, name):
        with self._lock:
            self._check_cancelled()
            self.containers.append(name)

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise ExecutionError('physical verification cancelled')
        if time.monotonic() >= self.deadline:
            raise ExecutionError('physical pipeline timed out')

    def _kill(self, process):
        # Cancellation may be requested by both branches concurrently. Signal
        # each group once; repeated signals racing with reaping can fail on macOS.
        with self._lock:
            if process not in self._signalled and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    if process.poll() is None:
                        raise
                self._signalled.add(process)

    def cancel(self):
        with self._lock:
            self._cancelled.set()
            for process in self._processes:
                self._kill(process)

    def run(self, command, label, *, cwd=None, env=None, on_poll=None):
        command = list(map(str, command))
        log = self.work / 'logs' / (label + '.log')
        record = {'stage': label, 'command': command, 'log': str(log),
                  'started_monotonic': time.monotonic()}
        process = None
        try:
            with log.open('w') as stream:
                with self._lock:
                    self._check_cancelled()
                    if any(c['stage'] == label for c in self.commands):
                        raise ExecutionError('duplicate physical stage: ' + label)
                    self.commands.append(record)
                    process = subprocess.Popen(command, cwd=cwd or self.work, env=env,
                        stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                    self._processes.add(process)
                while process.poll() is None:
                    self._check_cancelled()
                    if os.fstat(stream.fileno()).st_size > self.max_log_bytes:
                        raise ExecutionError(label + ' exceeded the stage-log limit')
                    with self._lock:
                        if time.monotonic() - self._last_scan > 5:
                            self._last_scan = time.monotonic()
                            violation = self.budget_error(self.work, log)
                            if violation:
                                raise ExecutionError(label + ' ' + violation)
                    if on_poll:
                        on_poll()
                    time.sleep(.1)
                self._check_cancelled()
                violation = self.budget_error(self.work, log)
                if violation:
                    raise ExecutionError(label + ' ' + violation)
                record['returncode'] = process.returncode
                if process.returncode:
                    raise ExecutionError(f'{label} exited {process.returncode}; see {log}')
            return log
        except BaseException as error:
            record['error'] = str(error)
            self.cancel()
            raise
        finally:
            if process is not None:
                self._kill(process)
                process.wait(timeout=20)
                record['returncode'] = process.returncode
                with self._lock:
                    self._processes.discard(process)
            record['elapsed_seconds'] = round(time.monotonic() - record['started_monotonic'], 6)

    def close(self):
        self.cancel()
        if not self.containers:
            return
        # A missing --rm container is normal. A remaining container or a Docker
        # query failure is not: no successful verdict can precede confirmed cleanup.
        subprocess.run(['docker', 'rm', '-f', *self.containers],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        remaining = set(subprocess.check_output(
            ['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True, timeout=20).splitlines())
        if remaining.intersection(self.containers):
            raise ExecutionError('Could not confirm physical container cleanup')


ARTIFACT_KEYS = {'gds': 'gds', 'netlist': 'nl', 'odb': 'odb', 'def': 'def', 'lef': 'lef'}


def seal_completed_artifacts(run_dir, destination, info_path):
    """Return None until the pinned flow completes Magic.WriteLEF.

    Step.start validates state_out and closes state_out.json before writing
    runtime.txt. A complete runtime marker is therefore the release barrier.
    This observes the same fresh flow; it never resumes a saved checkpoint.
    """
    run_dir, destination = Path(run_dir).resolve(), Path(destination)
    matches = list(run_dir.glob('[0-9]*-magic-writelef'))
    if len(matches) > 1:
        raise ExecutionError('ambiguous artifact release stage')
    if not matches:
        return None
    stage = matches[0]
    try:
        runtime = (stage / 'runtime.txt').read_text()
    except FileNotFoundError:
        return None
    if not re.fullmatch(r'\d+:\d{2}:\d{2}\.\d+', runtime):
        return None  # the trusted writer may still be writing the marker
    state = json.loads((stage / 'state_out.json').read_text())
    if any(destination.iterdir()):
        raise ExecutionError('sealed artifact directory must be fresh')
    artifacts = {}
    for key, state_key in ARTIFACT_KEYS.items():
        value = state.get(state_key)
        if not isinstance(value, str):
            raise ExecutionError('release state lacks ' + state_key)
        source = Path(value)
        if not source.is_absolute() or not source.resolve().is_relative_to(run_dir):
            raise ExecutionError('released artifact escapes the fresh flow')
        if any(p.is_symlink() for p in (source, *source.parents)):
            raise ExecutionError('released artifact uses a symlink')
        if not source.is_file() or source.stat().st_size == 0:
            raise ExecutionError('missing or empty released artifact: ' + key)
        target = destination / source.name
        if target.exists():
            raise ExecutionError('released artifacts have colliding names')
        before = digest(source)
        shutil.copyfile(source, target)
        if digest(target) != before or digest(source) != before:
            raise ExecutionError('artifact changed while sealing: ' + key)
        target.chmod(0o444)
        artifacts[key] = {'path': str(target.resolve()), 'sha256': before,
                          'source': str(source), 'release_state': str(stage / 'state_out.json')}
    info_target = destination / 'info.yaml'
    shutil.copyfile(info_path, info_target)
    info_target.chmod(0o444)
    return artifacts


def verify_sealed_artifacts(sealed, final):
    if set(sealed) != set(ARTIFACT_KEYS) or set(final) != set(ARTIFACT_KEYS):
        raise ExecutionError('incomplete sealed/final artifact set')
    for name, artifact in sealed.items():
        if digest(artifact['path']) != artifact['sha256']:
            raise ExecutionError('sealed artifact changed: ' + name)
        if digest(final[name]) != artifact['sha256']:
            raise ExecutionError('final artifact differs from checked artifact: ' + name)


def independent_checks(runner, checks):
    """Run two independent trusted checks; fail closed and join before returning.

    Check functions may launch only PhysicalRunner-owned subprocesses without
    preexec_fn. In particular candidate compilation must remain outside this
    thread scope. Return order follows the input order, not completion order.
    """
    if len(checks) != 2:
        raise ExecutionError('independent physical checks require exactly two branches')
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='physical-independent')
    try:
        futures = {pool.submit(check): index for index, check in enumerate(checks)}
        results = [None] * len(checks)
        for future in as_completed(futures):
            results[futures[future]] = future.result()
        return results
    except BaseException:
        # Includes report parsing and artifact failures after a command exits,
        # which do not pass through PhysicalRunner.run's cancellation handler.
        runner.cancel()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def overlap_checks(runner, harden_command, release_inputs, check_inputs):
    """Join both branches; either failure cancels and drains all subprocesses."""
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='physical-checks')
    future = None
    sealed = None

    def poll():
        nonlocal future, sealed
        if future is not None:
            if future.done():
                future.result()  # propagate side-branch failure while flow runs
            return
        sealed = release_inputs()
        if sealed is not None:
            future = pool.submit(check_inputs, sealed)

    try:
        runner.run(harden_command, 'harden', on_poll=poll)
        poll()
        if future is None:
            raise ExecutionError('fresh flow never released completed artifacts')
        return sealed, future.result()
    except BaseException:
        runner.cancel()
        raise
    finally:
        # No compiler with preexec_fn or later gate execution starts until the
        # scheduler thread has ended. Cancellation precedes draining the pool.
        pool.shutdown(wait=True, cancel_futures=True)
