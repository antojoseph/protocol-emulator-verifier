"""Scheduling negative controls use real subprocesses and trusted synthetic artifacts."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from verifier.physical_execution import (ARTIFACT_KEYS, ExecutionError, PhysicalRunner, independent_checks,
    overlap_checks, seal_completed_artifacts, verify_sealed_artifacts)


class PhysicalSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'logs').mkdir()
        self.runner = PhysicalRunner(self.root, time.monotonic() + 10, lambda *args: None, 4096)
        self.addCleanup(self.runner.cancel)

    def command(self, code):
        return [sys.executable, '-c', code]

    def test_branches_really_overlap_and_join(self):
        # Hardening cannot complete until the check branch signals it: a serial
        # scheduler would time out. No timing/speed assertion is needed.
        ready, done = self.root/'ready', self.root/'done'
        harden = self.command(f"from pathlib import Path; import time; Path({str(ready)!r}).touch()\nwhile not Path({str(done)!r}).exists(): time.sleep(.01)")
        def checks(inputs):
            self.runner.run(self.command(f"from pathlib import Path; Path({str(done)!r}).touch()"), 'checks')
            return ['all checks passed']
        sealed, checks = overlap_checks(self.runner, harden,
            lambda: {'ready': True} if ready.exists() else None, checks)
        self.assertEqual(sealed, {'ready': True})
        self.assertEqual(checks, ['all checks passed'])
        self.assertEqual({c['stage']: c['returncode'] for c in self.runner.commands}, {'harden': 0, 'checks': 0})
        self.assertFalse(self.runner._processes)

    def test_either_branch_failure_cancels_and_reaps_the_other(self):
        for fail_branch in ('harden', 'checks'):
            with self.subTest(fail_branch=fail_branch):
                runner = PhysicalRunner(self.root, time.monotonic()+5, lambda *args: None, 4096)
                ready = self.root / ('started-' + fail_branch)
                # Hardening waits for the branch to start before either fails.
                harden = self.command(f"from pathlib import Path; import time\nwhile not Path({str(ready)!r}).exists(): time.sleep(.01)\n" + ('raise SystemExit(7)' if fail_branch=='harden' else 'time.sleep(30)'))
                def checks(inputs):
                    runner.run(self.command(f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); " + ('raise SystemExit(8)' if fail_branch=='checks' else 'time.sleep(30)')), 'checks')
                with self.assertRaises(ExecutionError):
                    overlap_checks(runner, harden, lambda: {}, checks)
                self.assertFalse(runner._processes)
                self.assertEqual(len(runner.commands), 2)
                self.assertTrue(all(c['returncode'] != 0 for c in runner.commands))

    def test_shared_timeout_rejects_and_reaps(self):
        self.runner.deadline = time.monotonic() + .4
        with self.assertRaisesRegex(ExecutionError, 'timed out|cancelled'):
            overlap_checks(self.runner, self.command('import time; time.sleep(30)'),
                lambda: {}, lambda _: self.runner.run(self.command('import time; time.sleep(30)'), 'checks'))
        self.assertFalse(self.runner._processes)

    def test_missing_release_and_invalid_release_fail_closed(self):
        with self.assertRaisesRegex(ExecutionError, 'never released'):
            overlap_checks(self.runner, self.command('pass'), lambda: None, lambda _: self.fail('released too soon'))

    def test_log_and_aggregate_limits_fail_closed(self):
        with self.assertRaisesRegex(ExecutionError, 'stage-log'):
            self.runner.run(self.command("import time; print('x'*10000, flush=True); time.sleep(30)"), 'oversize')
        runner = PhysicalRunner(self.root, time.monotonic()+5, lambda *args: 'aggregate limit', 4096)
        with self.assertRaisesRegex(ExecutionError, 'aggregate limit'):
            runner.run(self.command('pass'), 'disk')
        self.assertFalse(runner._processes)

    def test_container_cleanup_only_removes_registered_names_and_confirms(self):
        self.runner.register_container('owned-test-container')
        with patch('verifier.physical_execution.subprocess.run') as remove, patch(
                'verifier.physical_execution.subprocess.check_output', return_value='unrelated\n'):
            self.runner.close()
            self.assertEqual(remove.call_args.args[0], ['docker', 'rm', '-f', 'owned-test-container'])
        with patch('verifier.physical_execution.subprocess.run'), patch(
                'verifier.physical_execution.subprocess.check_output', return_value='owned-test-container\n'):
            with self.assertRaisesRegex(ExecutionError, 'cleanup'):
                self.runner.close()
        with patch('verifier.physical_execution.subprocess.run'), patch(
                'verifier.physical_execution.subprocess.check_output', side_effect=subprocess.TimeoutExpired('docker', 20)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.runner.close()


class ThreeBranchSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'logs').mkdir()
        self.runner = PhysicalRunner(self.root, time.monotonic()+8, lambda *args: None, 4096)
        self.addCleanup(self.runner.cancel)

    def command(self, name, tail):
        ready = [str(self.root / (branch + '.ready')) for branch in ('harden', 'precheck', 'lvs')]
        code = (f"from pathlib import Path; import time; Path({str(self.root / (name+'.ready'))!r}).touch()\n"
                f"while not all(Path(p).exists() for p in {ready!r}): time.sleep(.01)\n" + tail)
        return [sys.executable, '-c', code]

    def schedule(self, tails, *, report_failure=None):
        def check(name):
            self.runner.run(self.command(name, tails[name]), name)
            if name == report_failure:
                raise ValueError('mandatory report failed: ' + name)
            return name
        return overlap_checks(self.runner, self.command('harden', tails['harden']), lambda: {},
            lambda _: independent_checks(self.runner, [lambda: check('precheck'), lambda: check('lvs')]))

    def test_all_three_overlap_and_join_with_stable_result_order(self):
        # All three children must reach the rendezvous before any can exit.
        # Precheck additionally waits for LVS to finish; result order remains fixed.
        finished = self.root / 'lvs.finished'
        tails = {'harden': 'pass', 'lvs': f'Path({str(finished)!r}).touch()',
                 'precheck': f'while not Path({str(finished)!r}).exists(): time.sleep(.01)'}
        _, results = self.schedule(tails)
        self.assertEqual(results, ['precheck', 'lvs'])
        self.assertEqual(len(self.runner.commands), 3)
        self.assertTrue(all(c['returncode'] == 0 for c in self.runner.commands))
        self.assertFalse(self.runner._processes)
        import threading
        self.assertFalse(any(t.name.startswith('physical-') for t in threading.enumerate()))

    def test_failure_in_each_branch_cancels_and_reaps_all(self):
        for fail in ('harden', 'precheck', 'lvs'):
            with self.subTest(fail=fail):
                for ready in self.root.glob('*.ready'):
                    ready.unlink()
                self.runner = PhysicalRunner(self.root, time.monotonic()+8, lambda *args: None, 4096)
                self.addCleanup(self.runner.cancel)
                tails = {name: 'raise SystemExit(7)' if name == fail else 'time.sleep(30)'
                         for name in ('harden', 'precheck', 'lvs')}
                with self.assertRaises(ExecutionError):
                    self.schedule(tails)
                self.assertEqual(len(self.runner.commands), 3)
                self.assertTrue(all(c['returncode'] != 0 for c in self.runner.commands))
                self.assertFalse(self.runner._processes)

    def test_report_failure_cancels_other_branches(self):
        for fail in ('precheck', 'lvs'):
            with self.subTest(fail=fail):
                for ready in self.root.glob('*.ready'):
                    ready.unlink()
                self.runner = PhysicalRunner(self.root, time.monotonic()+8, lambda *args: None, 4096)
                self.addCleanup(self.runner.cancel)
                tails = {name: 'pass' if name == fail else 'time.sleep(30)'
                         for name in ('harden', 'precheck', 'lvs')}
                with self.assertRaises((ExecutionError, ValueError)):
                    self.schedule(tails, report_failure=fail)
                self.assertEqual(len(self.runner.commands), 3)
                self.assertFalse(self.runner._processes)
                self.assertTrue(all(c['returncode'] != 0 for c in self.runner.commands if c['stage'] != fail))

    def test_shared_deadline_cancels_three_branches(self):
        self.runner.deadline = time.monotonic()+1
        with self.assertRaisesRegex(ExecutionError, 'timed out|cancelled'):
            self.schedule({name: 'time.sleep(30)' for name in ('harden', 'precheck', 'lvs')})
        self.assertEqual(len(self.runner.commands), 3)
        self.assertFalse(self.runner._processes)

    def test_report_failure_after_main_exit_cancels_lvs(self):
        import threading
        main_exited = threading.Event()
        real_run = self.runner.run
        def run(command, label, **kwargs):
            result = real_run(command, label, **kwargs)
            if label == 'harden':
                main_exited.set()
            return result
        def precheck():
            self.runner.run(self.command('precheck', 'pass'), 'precheck')
            self.assertTrue(main_exited.wait(3))
            raise ValueError('report rejected after flow completed')
        def lvs():
            self.runner.run(self.command('lvs', 'time.sleep(30)'), 'lvs')
        with patch.object(self.runner, 'run', side_effect=run):
            with self.assertRaisesRegex(ValueError, 'report rejected'):
                overlap_checks(self.runner, self.command('harden', 'pass'), lambda: {},
                    lambda _: independent_checks(self.runner, [precheck, lvs]))
        self.assertEqual({c['stage']: c['returncode'] for c in self.runner.commands}['harden'], 0)
        self.assertFalse(self.runner._processes)

    def test_aggregate_budget_covers_all_scratch_and_cancels(self):
        from verifier.physical import output_budget_error
        self.runner.budget_error = output_budget_error
        # Every branch writes below the same root. Each file is below the total
        # cap, but their combined size is not. Check at process completion.
        tails = {name: f"Path({str(self.root/name)!r}).write_bytes(b'x'*1024)"
                 for name in ('harden', 'precheck', 'lvs')}
        with patch('verifier.physical.MAX_GENERATED_BYTES', 1500):
            with self.assertRaisesRegex(ExecutionError, 'generated-output|cancelled'):
                self.schedule(tails)
        self.assertFalse(self.runner._processes)


class SealedArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.run_dir = self.root/'run'
        self.stage = self.run_dir/'61-magic-writelef'
        self.stage.mkdir(parents=True)
        self.dest = self.root/'sealed'
        self.dest.mkdir()
        self.info = self.root/'info.yaml'
        self.info.write_text('{}')
        self.state = {}
        for name, key in ARTIFACT_KEYS.items():
            path = self.stage/('chip.' + name)
            path.write_text(name)
            self.state[key] = str(path)
        self.write_state()

    def write_state(self):
        (self.stage/'state_out.json').write_text(json.dumps(self.state))

    def release(self):
        return seal_completed_artifacts(self.run_dir, self.dest, self.info)

    def complete(self):
        (self.stage/'runtime.txt').write_text('00:00:01.000')

    def test_no_release_before_complete_marker(self):
        self.assertIsNone(self.release())
        (self.stage/'runtime.txt').write_text('00:00:')
        self.assertIsNone(self.release())
        self.assertFalse(list(self.dest.iterdir()))
        self.complete()
        sealed = self.release()
        self.assertEqual(set(sealed), set(ARTIFACT_KEYS))
        verify_sealed_artifacts(sealed, {k: self.state[v] for k,v in ARTIFACT_KEYS.items()})
        self.assertTrue(all(Path(v['path']).stat().st_mode & 0o222 == 0 for v in sealed.values()))
        with self.assertRaisesRegex(ExecutionError, 'fresh'):
            self.release()

    def test_changed_final_or_sealed_content_rejected(self):
        self.complete()
        sealed = self.release()
        final = {k: self.state[v] for k,v in ARTIFACT_KEYS.items()}
        Path(final['gds']).write_text('different')
        with self.assertRaisesRegex(ExecutionError, 'final artifact differs'):
            verify_sealed_artifacts(sealed, final)
        path = Path(sealed['gds']['path'])
        path.chmod(0o644)
        path.write_text('different too')
        with self.assertRaisesRegex(ExecutionError, 'sealed artifact changed'):
            verify_sealed_artifacts(sealed, final)

    def test_missing_artifact_rejected(self):
        self.complete()
        del self.state['gds']
        self.write_state()
        with self.assertRaisesRegex(ExecutionError, 'lacks'):
            self.release()

    def test_external_and_symlink_artifacts_rejected(self):
        self.complete()
        self.state['gds'] = str(self.info)
        self.write_state()
        with self.assertRaisesRegex(ExecutionError, 'escapes'):
            self.release()
        link = self.stage/'link.gds'
        link.symlink_to(self.stage/'chip.gds')
        self.state['gds'] = str(link)
        self.write_state()
        with self.assertRaisesRegex(ExecutionError, 'symlink'):
            self.release()

    def test_copy_race_rejected(self):
        import shutil
        copy = shutil.copyfile
        def mutate(source, target):
            copy(source, target)
            Path(source).write_text('mutated')
        self.complete()
        with patch('verifier.physical_execution.shutil.copyfile', side_effect=mutate):
            with self.assertRaisesRegex(ExecutionError, 'changed while sealing'):
                self.release()


@unittest.skipUnless(__import__('os').environ.get('RUN_VERIFIER_INTEGRATION') == '1',
                     'set RUN_VERIFIER_INTEGRATION=1 for physical Docker cleanup tests')
class PhysicalDockerCleanupTests(unittest.TestCase):
    def test_three_container_timeout_cleans_up_every_owned_container(self):
        import uuid
        from verifier.physical import LOCK
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root/'logs').mkdir()
            runner = PhysicalRunner(root, time.monotonic()+5, lambda *args: None, 4096)
            names = ['janes-scheduler-test-' + uuid.uuid4().hex[:16] for _ in range(3)]
            def command(index):
                runner.register_container(names[index])
                return ['docker','run','--rm','--name',names[index],'--platform=linux/amd64',
                        '--network=none','--cap-drop=ALL','--security-opt=no-new-privileges',
                        '--read-only','--memory=256m','--cpus=1',LOCK['container'],
                        'python3','-c','import time; time.sleep(60)']
            try:
                with self.assertRaises(ExecutionError):
                    overlap_checks(runner, command(0), lambda: {},
                        lambda _: independent_checks(runner,
                            [lambda: runner.run(command(1), 'precheck'),
                             lambda: runner.run(command(2), 'lvs')]))
            finally:
                runner.close()
            self.assertEqual(len(runner.commands), 3)
            self.assertFalse(runner._processes)
            remaining = subprocess.check_output(['docker','ps','-a','--format','{{.Names}}'], text=True)
            self.assertTrue(all(name not in remaining.splitlines() for name in names))

    def test_concurrent_docker_timeout_leaves_no_owned_containers(self):
        import uuid
        from verifier.physical import LOCK
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            (root/'logs').mkdir()
            runner = PhysicalRunner(root, time.monotonic()+3, lambda *args: None, 4096)
            names = ['janes-scheduler-test-' + uuid.uuid4().hex[:16] for _ in range(2)]
            def command(name):
                runner.register_container(name)
                return ['docker','run','--rm','--name',name,'--platform=linux/amd64',
                        '--network=none','--cap-drop=ALL','--security-opt=no-new-privileges',
                        '--read-only','--memory=256m','--cpus=1',LOCK['container'],
                        'python3','-c','import time; time.sleep(60)']
            try:
                with self.assertRaises(ExecutionError):
                    overlap_checks(runner, command(names[0]), lambda: {},
                        lambda _: runner.run(command(names[1]), 'side'))
            finally:
                runner.close()
            self.assertEqual(len(runner.commands), 2)
            self.assertFalse(runner._processes)
            remaining = subprocess.check_output(['docker','ps','-a','--format','{{.Names}}'], text=True)
            self.assertTrue(all(name not in remaining.splitlines() for name in names))


if __name__ == '__main__':
    unittest.main()
