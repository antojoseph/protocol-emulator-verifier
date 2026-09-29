#!/usr/bin/env python3
"""Opt-in real-worker smoke check against a throwaway PostgreSQL database.

Uses the real Docker verifier, but a LOCAL artifact sink. No cloud resources accessed.
Run with ASIC_TEST_DATABASE_URL and the service requirements installed.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from service.client import pack
from service.core import Store
from service.worker import execute


def main():
    store = Store(os.environ['ASIC_TEST_DATABASE_URL'])
    store.initialize()
    base = (ROOT / '.runs/service-worker-smoke' / str(uuid.uuid4())).resolve()
    base.mkdir(parents=True)
    artifacts = base / 'local-test-artifacts'
    artifacts.mkdir()

    def uploader(directory, job, bucket):
        target = artifacts / str(job['id'])
        shutil.copytree(directory, target)
        return {'local_test_only': str(target)}

    def job_for(candidate):
        job_id = store.submit(pack(candidate), 'fast', str(uuid.uuid4()))
        job = store.claim('fast')
        assert job is not None and str(job['id']) == job_id, 'Use an empty throwaway database'
        return job_id, job

    good_id, good = job_for(ROOT / 'candidates/tempo')
    execute(store, good, base, 'LOCAL-TEST-ONLY', uploader=uploader)
    row = store.get(good_id)
    assert row['state'] == 'completed', row
    assert row['result']['status'] == 'pass' and row['result']['accepted'] is False
    assert row['result']['score'] is None and row['artifact']['local_test_only']
    bad = base / 'bad-candidate'
    shutil.copytree(ROOT / 'candidates/tempo', bad)
    # A real unsafe-HDL rejection, not an overridden expected answer.
    with (bad / 'src/tt_um_protocol_emulator.v').open('a') as file:
        file.write('\ninitial $finish;\n')
    bad_id, bad_job = job_for(bad)
    execute(store, bad_job, base, 'LOCAL-TEST-ONLY', uploader=uploader)
    rejected = store.get(bad_id)
    assert rejected['state'] == 'completed' and rejected['result']['accepted'] is False, rejected
    assert rejected['result']['status'] == 'fail'

    cancel_id, cancel_job = job_for(ROOT / 'candidates/tempo')
    def cancel():
        time.sleep(2)
        store.cancel(cancel_id)
    canceller = threading.Thread(target=cancel)
    canceller.start()
    execute(store, cancel_job, base, 'LOCAL-TEST-ONLY', uploader=uploader)
    canceller.join()
    cancelled = store.get(cancel_id)
    assert cancelled['state'] == 'cancelled' and cancelled['result'] is None, cancelled
    report = {'real_fast_baseline': good_id, 'real_unsafe_hdl_rejected': bad_id,
              'running_cancellation': cancel_id, 'artifact_sink': 'local test only', 'passed': True}
    (base / 'summary.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'report': str(base / 'summary.json'), **report}, indent=2))


if __name__ == '__main__':
    main()
