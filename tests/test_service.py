"""Hosting boundary tests; database tests require an isolated ASIC_TEST_DATABASE_URL."""
import copy
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
from unittest.mock import MagicMock
import subprocess

from service.client import pack
from service.core import ROOT, MAX_BYTES, Store, classify, unpack


def archive(entries):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for name, body in entries:
            z.writestr(name, body)
    return out.getvalue()


class ArchiveTests(unittest.TestCase):
    def test_roundtrip_baseline(self):
        from verifier.candidate import validate
        blob = pack(ROOT / 'candidates/tempo')
        with tempfile.TemporaryDirectory() as temp:
            identity, hashes = unpack(blob, Path(temp) / 'candidate')
            self.assertEqual(hashes, validate(Path(temp) / 'candidate')[1])
            self.assertEqual(identity, unpack(blob)[0])

    def test_rejects_traversal_hidden_duplicate_and_links(self):
        for name in ('../escape', '/tmp/escape', 'x/../../escape', '.git/config',
                     'x//y', 'x/./y', 'x\\y', 'x:y', 'candidate.json'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                unpack(archive([('candidate.json', '{}'), (name, 'bad')]))
        info = zipfile.ZipInfo('link')
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with self.assertRaises(ValueError):
            unpack(archive([('candidate.json', '{}'), (info, '/etc/passwd')]))

    def test_rejects_expansion_bomb(self):
        with self.assertRaisesRegex(ValueError, 'Expanded'):
            unpack(archive([('candidate.json', '{}'), ('huge', b'0' * (MAX_BYTES + 1))]))


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.result = json.loads((ROOT / 'reports/baseline-full.json').read_text())
        self.hashes = self.result['candidate']['source_sha256']
        self.seed = self.result['seed']

    def test_existing_accepted_baseline_is_recognized(self):
        self.assertEqual(classify(self.result, 'full', 0, self.hashes, self.seed), 'completed')

    def test_incomplete_or_forged_acceptance_fails_closed(self):
        mutations = [lambda r: r['stages'].pop('gates'),
                     lambda r: r.update(score=float('nan')),
                     lambda r: r.update(score=True),
                     lambda r: r.update(score=999),
                     lambda r: r.update(accepted=False),
                     lambda r: r.update(checks=[]),
                     lambda r: r.update(harness_sha256={}),
                     lambda r: r.update(seed=0),
                     lambda r: r['candidate'].update(source_sha256={})]
        for mutation in mutations:
            result = copy.deepcopy(self.result)
            mutation(result)
            with self.assertRaises(ValueError):
                classify(result, 'full', 0, self.hashes, self.seed)
        with self.assertRaises(ValueError):
            classify(self.result, 'full', 1, self.hashes, self.seed)

    def test_fast_can_never_publish_acceptance(self):
        self.result['mode'] = 'fast'
        with self.assertRaises(ValueError):
            classify(self.result, 'fast', 0, self.hashes, self.seed)
        self.result.update(accepted=False, score=None)
        self.assertEqual(classify(self.result, 'fast', 0, self.hashes, self.seed), 'completed')


class DeploymentGuardTests(unittest.TestCase):
    def test_existing_project_is_rejected_before_any_write(self):
        from infra.gcp.deploy import Deployment
        runner = Mock(return_value=subprocess.CompletedProcess([], 0,
            json.dumps([{'projectId': 'asic-verifier-existing'}]), ''))
        deployment = Deployment('/unused', {'account': 'operator@example.com',
            'project': 'asic-verifier-existing', 'nonce': 'abc'}, runner=runner)
        with self.assertRaisesRegex(ValueError, 'Existing projects'):
            deployment.create()
        self.assertEqual(runner.call_count, 1)
        self.assertIn('list', runner.call_args[0][0])

    def test_failed_discovery_is_not_permission_to_create(self):
        from infra.gcp.deploy import Deployment
        runner = Mock(return_value=subprocess.CompletedProcess([], 1, '', 'Authentication failed'))
        deployment = Deployment('/unused', {'account': 'operator@example.com',
            'project': 'asic-verifier-new'}, runner=runner)
        with self.assertRaisesRegex(RuntimeError, 'Authentication failed'):
            deployment.create()
        self.assertEqual(runner.call_count, 1)

    def test_resume_requires_project_number_and_ownership_label(self):
        from infra.gcp.deploy import Deployment
        runner = Mock(return_value=subprocess.CompletedProcess([], 0,
            json.dumps({'projectNumber': '123', 'labels': {'asic-isolation': 'somebody-else'}}), ''))
        deployment = Deployment('/unused', {'account': 'operator@example.com',
            'project': 'asic-verifier-new', 'number': '123', 'nonce': 'our-project', 'completed': []}, runner=runner)
        with self.assertRaisesRegex(ValueError, 'not owned'):
            deployment.step('network', 'compute', 'networks', 'create', 'asic-only')
        self.assertEqual(runner.call_count, 1)
        command = runner.call_args[0][0]
        self.assertIn('--project=asic-verifier-new', command)
        self.assertIn('--account=operator@example.com', command)


class CleanupTests(unittest.TestCase):
    def test_only_this_attempts_containers_are_removed(self):
        from service.worker import cleanup_containers
        base = Path('/tmp/asic-attempt').resolve()
        def run(command, **kwargs):
            if command[:2] == ['docker', 'inspect']:
                source = str(base / 'run') if command[2] == 'ours' else '/other-project/data'
                return subprocess.CompletedProcess(command, 0, json.dumps([{'Mounts': [{'Source': source}]}]), '')
            return subprocess.CompletedProcess(command, 0, '', '')
        with patch('service.worker.subprocess.check_output', return_value='ours\nother\n'), \
             patch('service.worker.subprocess.run', side_effect=run) as invoke:
            cleanup_containers(base)
        removals = [call.args[0] for call in invoke.call_args_list if call.args[0][1] == 'rm']
        self.assertEqual(removals, [['docker', 'rm', '-f', 'ours']])

    def test_inspect_failure_is_not_mistaken_for_successful_cleanup(self):
        from service.worker import cleanup_containers
        with patch('service.worker.subprocess.check_output', return_value='still-present\n'), \
             patch('service.worker.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'daemon error')):
            with self.assertRaisesRegex(RuntimeError, 'still-present'):
                cleanup_containers(Path('/tmp/asic-attempt'))


class ApiBoundaryTests(unittest.TestCase):
    def test_loopback_operator_access_and_browser_rejection(self):
        from http.server import HTTPServer
        from service.api import Handler
        server = HTTPServer(('127.0.0.1', 0), Handler)
        server.store = MagicMock()
        thread = threading.Thread(target=server.serve_forever,
                                  kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port) + '/health'
        try:
            request = urllib.request.Request(url, headers={'X-ASIC-Client': '1'})
            with urllib.request.urlopen(request, timeout=3) as response:
                self.assertEqual(json.load(response), {'status': 'ok'})
            for headers in ({},
                    {'X-ASIC-Client': '1', 'Host': 'rebound.example:18124'},
                    {'X-ASIC-Client': '1', 'Origin': 'http://attacker.example'},
                    {'X-ASIC-Client': '1', 'Host': 'localhost.attacker.example'}):
                with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3)
                self.assertEqual(failure.exception.code, 403)
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()


@unittest.skipUnless(os.environ.get('ASIC_TEST_DATABASE_URL'), 'isolated PostgreSQL required')
class QueueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = Store(os.environ['ASIC_TEST_DATABASE_URL'])
        cls.store.initialize()
        cls.blob = pack(ROOT / 'candidates/tempo')

    def setUp(self):
        # This URL MUST identify a throwaway database, never a deployment database.
        with self.store.connection() as db:
            db.execute('TRUNCATE asic_jobs')

    def submit(self, mode='full', key=None):
        return self.store.submit(self.blob, mode, key or str(uuid.uuid4()))

    def expire(self, job):
        with self.store.connection() as db:
            db.execute("UPDATE asic_jobs SET lease_until=now()-interval '1 second' WHERE id=%s", (job,))

    def test_idempotency_and_changed_request_rejection(self):
        key = str(uuid.uuid4())
        first = self.submit(key=key)
        self.assertEqual(first, self.submit(key=key))
        with self.assertRaises(ValueError):
            self.submit(mode='fast', key=key)

    def test_concurrent_claim_is_exclusive(self):
        self.submit()
        with ThreadPoolExecutor(max_workers=8) as executor:
            rows = list(executor.map(lambda _: self.store.claim('full'), range(8)))
        self.assertEqual(sum(row is not None for row in rows), 1)

    def test_expired_worker_cannot_publish_or_renew_after_reclaim(self):
        job = self.submit()
        old = self.store.claim('full')
        self.expire(job)
        self.assertFalse(self.store.finish(job, old['lease_token'], 'completed'))
        new = self.store.claim('full')
        self.assertEqual(new['seed'], old['seed'])
        self.assertEqual(new['source_hash'], old['source_hash'])
        self.assertNotEqual(old['lease_token'], new['lease_token'])
        self.assertIsNone(self.store.heartbeat(job, old['lease_token']))
        self.assertFalse(self.store.finish(job, old['lease_token'], 'completed'))
        self.assertTrue(self.store.finish(job, new['lease_token'], 'completed', result={'accepted': False}))
        self.assertFalse(self.store.finish(job, new['lease_token'], 'completed'))

    def test_cancellation_race_never_publishes_acceptance(self):
        job = self.submit()
        lease = self.store.claim('full')
        self.assertTrue(self.store.cancel(job))
        self.assertTrue(self.store.heartbeat(job, lease['lease_token']))
        self.store.finish(job, lease['lease_token'], 'completed', result={'accepted': True})
        row = self.store.get(job)
        self.assertEqual(row['state'], 'cancelled')
        self.assertIsNone(row['result'])

    def test_queued_cancel_is_never_claimed(self):
        job = self.submit()
        self.store.cancel(job)
        self.assertIsNone(self.store.claim('full'))

    def test_crash_retries_are_bounded(self):
        job = self.submit()
        for n in range(3):
            self.assertEqual(self.store.claim('full')['attempt'], n + 1)
            self.expire(job)
        self.assertIsNone(self.store.claim('full'))
        self.assertEqual(self.store.get(job)['state'], 'failed')

    def test_separate_lanes(self):
        job = self.submit('fast')
        self.assertIsNone(self.store.claim('full'))
        self.assertEqual(str(self.store.claim('fast')['id']), job)


if __name__ == '__main__':
    unittest.main()
