"""Bounded spawned compiler workers; no shared candidate container or entropy."""
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
import multiprocessing
from pathlib import Path
import secrets
import shutil
import subprocess

from .common import ToolUnavailable, VerificationError


class CompilerBatchError(VerificationError):
    def __init__(self, index, cause):
        self.index = index
        self.blocked = isinstance(cause, ToolUnavailable)
        super().__init__(str(cause))


def _invoke(candidate, manifest, workdir, request, name):
    # Runs on the main thread of a fresh spawned Python worker. Existing
    # compiler preexec limits must never run in a ThreadPoolExecutor thread.
    from .isolation import compiler
    return compiler(candidate, manifest, workdir, _container_name=name)(request)


def _cleanup_owned(docker, names):
    """After every worker joins, remove only this batch's preassigned names."""
    def existing():
        try:
            result = subprocess.run([docker, 'ps', '-a', '--format', '{{.Names}}'],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError('Could not confirm compiler batch cleanup') from error
        if result.returncode:
            raise VerificationError('Could not confirm compiler batch cleanup')
        return set(result.stdout.splitlines()) & set(names)
    remaining = existing()
    if remaining:
        try:
            result = subprocess.run([docker, 'rm', '--force', *sorted(remaining)],
                                    capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise VerificationError('Compiler batch cleanup failed') from error
        if result.returncode:
            raise VerificationError('Compiler batch cleanup failed')
    if existing():
        raise VerificationError('Compiler batch containers remain after cleanup')


def compile_batch(candidate, manifest, workdir, requests, *, workers=4):
    if type(workers) is not int or workers not in (2, 4):
        raise ValueError('batch compiler workers must be 2 or 4')
    if not isinstance(requests, list) or not 1 <= len(requests) <= 43:
        raise ValueError('expected 1..43 compiler requests')
    docker = shutil.which('docker')
    if not docker:
        raise ToolUnavailable('Docker is required for compiler batches')
    directory = Path(workdir).resolve() / 'batch'
    directory.mkdir(parents=True, exist_ok=False)
    prefix = 'protocol-compiler-' + secrets.token_hex(12)
    names = [prefix + '-%02d' % i for i in range(len(requests))]
    responses = [None] * len(requests)
    pending = {}
    pool = None
    try:
        pool = ProcessPoolExecutor(max_workers=workers,
                                   mp_context=multiprocessing.get_context('spawn'))
        next_index = 0
        while pending or next_index < len(requests):
            while len(pending) < workers and next_index < len(requests):
                i = next_index
                future = pool.submit(_invoke, str(candidate), manifest,
                                     str(directory / ('case-%02d' % i)), requests[i], names[i])
                pending[future] = i
                next_index += 1
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in sorted(done, key=lambda f: pending[f]):
                index = pending.pop(future)
                try:
                    responses[index] = future.result()
                except Exception as error:
                    raise CompilerBatchError(index, error) from error
    finally:
        # No fresh private inputs may be created until this returns. Failures
        # stop dispatch, cancel pending work, and drain already-running calls
        # under their unchanged 25-second compiler / 10-second cleanup bounds.
        for future in pending:
            future.cancel()
        try:
            if pool is not None:
                pool.shutdown(wait=True, cancel_futures=True)
        finally:
            _cleanup_owned(docker, names)
    return responses
