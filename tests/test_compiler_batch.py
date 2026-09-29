import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from verifier import compiler_batch as batch
from verifier.candidate import validate
from verifier.common import ROOT, VerificationError
from verifier.functional import run_functional
from verifier.isolation import compiler


def spawned_fixture(candidate, manifest, workdir, request, name):
    path = Path(workdir)
    path.mkdir(parents=True)
    started = time.monotonic()
    (path / 'started').write_text(str(started))
    try:
        time.sleep(request.get('delay', .15))
        if request.get('crash'):
            os._exit(3)
        if request.get('fail'):
            raise VerificationError('fixture failure')
        return {'index': request['index'], 'container': name}
    finally:
        (path / 'finished').write_text(str(time.monotonic()))


class CompilerBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def execute(self, requests, cleanup=None):
        with patch.object(batch, '_invoke', spawned_fixture), \
             patch.object(batch.shutil, 'which', return_value='/fake/docker'), \
             patch.object(batch, '_cleanup_owned', side_effect=cleanup) as cleaned:
            result = batch.compile_batch('/unused', {}, self.root / 'work', requests)
        self.assertEqual(cleaned.call_count, 1)
        return result

    def test_spawned_parallelism_is_bounded_and_results_keep_input_order(self):
        requests = [{'index': i, 'delay': .3 if i == 0 else .1} for i in range(9)]
        result = self.execute(requests)
        self.assertEqual([r['index'] for r in result], list(range(9)))
        self.assertEqual(len({r['container'] for r in result}), 9)
        events = []
        for path in (self.root / 'work/batch').iterdir():
            events.extend([(float((path/'started').read_text()), 1),
                           (float((path/'finished').read_text()), -1)])
        active = maximum = 0
        for _, change in sorted(events):
            active += change
            maximum = max(maximum, active)
        self.assertGreater(maximum, 1)
        self.assertLessEqual(maximum, 4)
        self.assertEqual(active, 0)

    def test_failure_stops_dispatch_and_drains_before_cleanup(self):
        requests = [{'index': i, 'fail': i == 0, 'delay': .01 if i == 0 else .25}
                    for i in range(10)]
        def cleanup(docker, names):
            started = list((self.root/'work/batch').glob('*/started'))
            self.assertLessEqual(len(started), 4)
            self.assertTrue(all(p.with_name('finished').exists() for p in started))
            self.assertEqual(len(names), 10)
        with self.assertRaises(batch.CompilerBatchError) as error:
            self.execute(requests, cleanup)
        self.assertEqual(error.exception.index, 0)

    def test_worker_crash_still_invokes_owned_container_cleanup(self):
        with patch.object(batch, '_invoke', spawned_fixture), \
             patch.object(batch.shutil, 'which', return_value='/fake/docker'), \
             patch.object(batch, '_cleanup_owned') as cleanup:
            with self.assertRaises(batch.CompilerBatchError):
                batch.compile_batch('/unused', {}, self.root/'work',
                                    [{'index': 0, 'crash': True}])
        cleanup.assert_called_once()

    def test_cleanup_failure_cannot_return_success(self):
        with self.assertRaisesRegex(VerificationError, 'cleanup'):
            self.execute([{'index': 0}], lambda *_: (_ for _ in ()).throw(VerificationError('cleanup failed')))

    def test_bad_worker_counts_and_reused_batch_directory_are_rejected(self):
        for workers in (True, 0, 3, 8):
            with self.assertRaises(ValueError):
                batch.compile_batch('/unused', {}, self.root/'work', [{'index': 0}], workers=workers)
        self.execute([{'index': 0}])
        with self.assertRaises(FileExistsError):
            self.execute([{'index': 0}])

    def test_cleanup_removes_only_owned_names_and_checks_again(self):
        from subprocess import CompletedProcess
        responses = [CompletedProcess([], 0, 'owned\nunrelated\n', ''),
                     CompletedProcess([], 0, 'owned\n', ''),
                     CompletedProcess([], 0, 'unrelated\n', '')]
        with patch.object(batch.subprocess, 'run', side_effect=responses) as run:
            batch._cleanup_owned('/docker', ['owned', 'not-created'])
        self.assertEqual(run.call_args_list[1].args[0], ['/docker','rm','--force','owned'])

    def test_cleanup_daemon_failure_or_remaining_container_fails_closed(self):
        from subprocess import CompletedProcess
        for responses in ([CompletedProcess([], 1, '', 'unavailable')],
                          [CompletedProcess([], 0, 'owned\n', '')]*3):
            with patch.object(batch.subprocess, 'run', side_effect=responses):
                with self.assertRaises(VerificationError):
                    batch._cleanup_owned('/docker', ['owned'])

    def test_failed_batch_generates_no_private_inputs_or_simulator_job(self):
        candidate = ROOT/'candidates/tempo'
        manifest, _ = validate(candidate)
        def adapter(_):
            raise AssertionError('serial compiler must not be called')
        def failure(_):
            raise batch.CompilerBatchError(3, VerificationError('bad compiler'))
        adapter.batch = failure
        with patch('verifier.functional.secrets.randbits') as entropy, \
             patch('verifier.functional.run_eda') as eda:
            result = run_functional(candidate, manifest, self.root/'functional', adapter_runner=adapter)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(result['checks'][-1]['name'], 'adapter_case_3')
        entropy.assert_not_called()
        eda.assert_not_called()
        self.assertFalse((self.root/'functional/private_replay.json').exists())


@unittest.skipUnless(os.environ.get('RUN_VERIFIER_INTEGRATION') == '1', 'Docker integration disabled')
class BatchDockerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.candidate = self.root/'candidate'
        shutil.copytree(ROOT/'candidates/tempo', self.candidate)

    def test_real_batch_matches_serial_adapter_outputs(self):
        from verifier.functional import _public_workloads
        manifest, _ = validate(self.candidate)
        requests = [{'schema_version': 1, 'clock_hz': 50000000,
                     **{k:v for k,v in r.items() if k not in ('nack_index','address_nack')}}
                    for r in _public_workloads(20260928)[:4]]
        serial = compiler(self.candidate, manifest, self.root/'serial')
        expected = [serial(r) for r in requests]
        actual = compiler(self.candidate, manifest, self.root/'parallel', workers=4).batch(requests)
        self.assertEqual(actual, expected)

    def test_failure_and_timeout_remove_all_owned_containers(self):
        adapter = self.candidate/'adapter.py'
        adapter.write_text('import json,sys,time\nr=json.load(sys.stdin)\n'
                           'if r["index"] == 0:\n time.sleep(1)\n raise RuntimeError("deliberate failure")\n'
                           'time.sleep(60)\nprint("{}")\n')
        manifest, _ = validate(self.candidate)
        # compile_batch's final Docker inventory check must run even on error.
        # Verify independently using a unique prefix supplied to the parent.
        with patch.object(batch.secrets, 'token_hex', return_value='boundarycheckbatch'):
            with self.assertRaises(batch.CompilerBatchError):
                compiler(self.candidate, manifest, self.root/'again', workers=2).batch(
                    [{'index': 0}, {'index': 1}])
        import subprocess
        result = subprocess.run(['docker','ps','-a','--filter','name=protocol-compiler-boundarycheckbatch','--format','{{.Names}}'],capture_output=True,text=True,check=True)
        self.assertEqual(result.stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
