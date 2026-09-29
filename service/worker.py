"""One leased evaluation per process. All candidate execution stays in the existing harness."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import threading
import time

from .core import ROOT, Store, classify, unpack, verifier_identity


def cleanup_containers(directory):
    """Only remove containers mounting this attempt's directory, never other jobs."""
    directory = Path(directory).resolve()
    names = subprocess.check_output(['docker', 'ps', '-aq'], text=True, timeout=20).split()
    for name in names:
        detail = subprocess.run(['docker', 'inspect', name], capture_output=True, text=True, timeout=20)
        if detail.returncode:
            # A --rm container may disappear between list and inspect.
            remaining = subprocess.check_output(['docker', 'ps', '-aq'], text=True, timeout=20).split()
            if name in remaining:
                raise RuntimeError('Cannot inspect a still-present container during cleanup')
            continue
        info = json.loads(detail.stdout)[0]
        if any(Path(m.get('Source', '/')).is_relative_to(directory) for m in info.get('Mounts', [])):
            subprocess.run(['docker', 'rm', '-f', name], check=True, capture_output=True, timeout=30)


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def upload(directory, job, bucket_name):
    from google.cloud import storage
    target = directory / 'evidence.tar.gz'
    with tarfile.open(target, 'w:gz', compresslevel=1, dereference=False) as archive:
        for child in sorted(directory.iterdir()):
            if child != target:
                archive.add(child, arcname=child.name, recursive=True)
    checksum = sha256(target)
    name = f"jobs/{job['id']}/{job['lease_token']}/evidence.tar.gz"
    blob = storage.Client().bucket(bucket_name).blob(name)
    blob.metadata = {'sha256': checksum, 'source_hash': job['source_hash'],
                     'verifier_hash': job['verifier_hash']}
    blob.upload_from_filename(str(target), if_generation_match=0, timeout=120, checksum='crc32c')
    return {'uri': f'gs://{bucket_name}/{name}', 'generation': str(blob.generation),
            'sha256': checksum, 'bytes': target.stat().st_size}


def execute(store, job, base, bucket, *, uploader=upload):
    directory = base / str(job['id']) / str(job['lease_token'])
    directory.mkdir(parents=True, exist_ok=False)
    directory.chmod(0o755)
    stop = threading.Event()
    abort = threading.Event()
    reason = []

    def heartbeats():
        while not stop.is_set():
            try:
                cancelled = store.heartbeat(job['id'], job['lease_token'])
                if cancelled is None or cancelled:
                    reason.append('Lease lost' if cancelled is None else 'Cancelled')
                    abort.set()
                    return
            except Exception:
                reason.append('Cannot renew worker lease')
                abort.set()
                return
            stop.wait(15)

    thread = threading.Thread(target=heartbeats, daemon=True)
    thread.start()
    process = None
    cleanup_ok = False
    try:
        if job['verifier_hash'] != verifier_identity():
            raise ValueError('Queued verifier revision differs from this worker')
        blob = bytes(job['archive'])
        (directory / 'submission.zip').write_bytes(blob)
        source_hash, hashes = unpack(blob, directory / 'candidate')
        if source_hash != job['source_hash']:
            raise ValueError('Stored candidate digest mismatch')
        (directory / 'identity.json').write_text(json.dumps({
            'job': str(job['id']), 'attempt': str(job['lease_token']),
            'source_hash': source_hash, 'verifier_hash': job['verifier_hash'],
            'mode': job['mode'], 'seed': job['seed']}, indent=2))
        command = ['/usr/bin/python3', str(ROOT / 'verify'), str(directory / 'candidate'),
                   '--mode', job['mode'], '--seed', str(job['seed']), '--out', str(directory / 'run')]
        started = time.monotonic()
        limit = 21600 if job['mode'] == 'full' else 1800
        with (directory / 'worker.log').open('w') as log:
            process = subprocess.Popen(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True, env={'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'HOME': str(directory), 'PYTHONDONTWRITEBYTECODE': '1', 'LC_ALL': 'C.UTF-8'})
            checked = 0
            while process.poll() is None:
                if abort.wait(1) or time.monotonic() - started > limit:
                    raise RuntimeError(reason[0] if reason else 'Overall verification deadline exceeded')
                if time.monotonic() - checked > 15:
                    checked = time.monotonic()
                    size = sum(p.lstat().st_size for p in directory.rglob('*') if not p.is_symlink())
                    if size > 22 * 1024**3 or shutil.disk_usage(base).free < 10 * 1024**3:
                        raise RuntimeError('Worker disk budget exceeded')
        cleanup_containers(directory)
        cleanup_ok = True
        if abort.is_set():
            raise RuntimeError(reason[0])
        report = directory / 'run/result.json'
        if report.stat().st_size > 8 * 1024 * 1024:
            raise ValueError('Result report exceeds size limit')
        from verifier.common import read_json
        result = read_json(report)
        if job['verifier_hash'] != verifier_identity():
            raise ValueError('Worker verifier changed during evaluation')
        state = classify(result, job['mode'], process.returncode, hashes, job['seed'])
        # Keep renewing the lease while evidence uploads; acceptance follows upload success.
        artifact = uploader(directory, job, bucket)
        if abort.is_set():
            raise RuntimeError(reason[0])
        committed = store.finish(job['id'], job['lease_token'], state, result=result, artifact=artifact)
        if committed:
            shutil.rmtree(directory)
    except Exception as error:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
        try:
            cleanup_containers(directory)
            cleanup_ok = True
        finally:
            store.finish(job['id'], job['lease_token'], 'failed', error=str(error)[:2000])
        # Preserve bounded local diagnostics on infrastructure failure.
    finally:
        stop.set()
        thread.join(timeout=20)
        if not cleanup_ok:
            raise RuntimeError('Container cleanup failed; stop this worker before accepting another job')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=('fast', 'full'), required=True)
    args = parser.parse_args()
    store = Store(os.environ['ASIC_DATABASE_URL'])
    base = Path(os.environ['ASIC_RUNS']) / args.mode
    base.mkdir(parents=True, exist_ok=True)
    # Prevent service restarts or operator commands starting a second local worker for a lane.
    with (base / '.worker.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cleanup_containers(base)
        while True:
            if shutil.disk_usage(base).free < 40 * 1024**3:
                print('Insufficient scratch space; admissions paused', flush=True)
                time.sleep(15)
                continue
            job = store.claim(args.mode)
            if job:
                execute(store, job, base, os.environ['ASIC_BUCKET'])
            else:
                time.sleep(2)


if __name__ == '__main__':
    main()
