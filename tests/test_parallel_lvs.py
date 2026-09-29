"""Synthetic physical orchestration/mount controls; these are not EDA acceptance."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

from verifier.physical import LOCK, PRECHECKS, run_physical
from verifier.physical_execution import ARTIFACT_KEYS, digest


class ParallelLVSOrchestrationTests(unittest.TestCase):
    def exercise(self, schedule, *, tamper_cdl=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            candidate, work = root/'candidate', root/'output'
            candidate.mkdir()
            (candidate/'chip.v').write_text('module tt_um_chip(); endmodule')
            (candidate/'candidate.json').write_text(json.dumps(
                {'top_module': 'tt_um_chip', 'rtl_files': ['chip.v']}))
            commands, command_lock = {}, threading.Lock()
            branch_rendezvous = threading.Barrier(2, timeout=3)

            def copy_tracked(source, destination):
                (destination/'precheck').mkdir(parents=True)

            def release(run_dir, destination, info):
                sealed = {}
                for name in ARTIFACT_KEYS:
                    path = destination/('chip.'+name)
                    path.write_text(name)
                    path.chmod(0o444)
                    sealed[name] = {'path': str(path), 'sha256': digest(path)}
                return sealed

            def run(runner, command, label, **kwargs):
                with command_lock:
                    self.assertNotIn(label, commands)
                    commands[label] = list(map(str, command))
                path = work/'logs'/(label+'.log')
                path.write_text('')
                if label == 'harden':
                    if kwargs.get('on_poll'):
                        kwargs['on_poll']()
                elif label == 'official_precheck':
                    if schedule == 'parallel-lvs':
                        branch_rendezvous.wait()
                    suite = ET.Element('testsuite')
                    for name in PRECHECKS:
                        ET.SubElement(suite, 'testcase', name=name)
                    ET.ElementTree(suite).write(work/'checks/tt/precheck/reports/results.xml')
                elif label == 'gds_geometry':
                    (work/'checks/gds_geometry.json').write_text(json.dumps({'bbox': LOCK['die_um']}))
                elif label == 'export_cdl':
                    if schedule == 'parallel-lvs':
                        branch_rendezvous.wait()
                    (work/'lvs_checks/gds_lvs/routed.cdl').write_text('* synthetic CDL\n')
                elif label == 'gds_lvs':
                    path.write_text('Devices to compare: layout=1 schematic=1\nCongratulations! Netlists match.\n')
                    cdl = work/'lvs_checks/trusted/routed.cdl'
                    self.assertFalse(cdl.stat().st_mode & 0o222)
                    if tamper_cdl:
                        cdl.chmod(0o644)
                        cdl.write_text('changed')
                return path

            with ExitStack() as stack:
                for target, kwargs in [
                    ('_repository_verified', {}), ('_check_ports', {}),
                    ('_copy_tracked', {'side_effect': copy_tracked}),
                    ('verify_precheck_wheels', {'return_value': root/'wheels'}),
                    ('seal_completed_artifacts', {'side_effect': release}),
                    ('subprocess.run', {}),
                    ('PhysicalRunner.close', {}),
                    ('PhysicalRunner.run', {'new': run}),
                ]:
                    stack.enter_context(patch('verifier.physical.'+target, **kwargs))
                result = run_physical(candidate, work, {'schedule': schedule, 'tools_root': str(root/'tools')})
            self.assertEqual(result['status'], 'fail')
            self.assertIn('CDL changed' if tamper_cdl else 'did not reach final manufacturability',
                          result['checks'][-1]['detail'])
            self.assertIsNone(result['gate_netlist'])  # Synthetic partial evidence must not pass.
            required_commands = {'precheck_dependencies', 'ports', 'harden',
                                 'official_precheck', 'gds_geometry', 'export_cdl', 'gds_lvs'}
            if tamper_cdl:
                # Cancellation may prevent the sibling's next command.
                self.assertTrue(required_commands - {'gds_geometry'} <= set(commands) <= required_commands)
            else:
                self.assertEqual(set(commands), required_commands)
            expected_cap = {'serial': 1, 'parallel': 2, 'parallel-lvs': 3}[schedule]
            self.assertEqual(result['provenance']['execution']['max_concurrent_containers'], expected_cap)
            names = set()
            for label, command in commands.items():
                names.add(command[command.index('--name')+1])
                mounts = [command[i+1] for i, value in enumerate(command) if value == '--volume']
                self.assertIn(f'{work}/sealed:{work}/sealed:ro', mounts)
                self.assertIn('--memory=16g', command)
                self.assertIn('--cpus=4', command)
                self.assertIn('--pids-limit=2048', command)
                if label in ('official_precheck', 'gds_geometry'):
                    self.assertIn(f'{work}/checks:{work}/checks', mounts)
                    self.assertFalse(any(m.startswith(f'{work}:') or 'lvs_checks' in m for m in mounts))
                elif label in ('export_cdl', 'gds_lvs'):
                    self.assertIn(f'{work}/lvs_checks:{work}/lvs_checks', mounts)
                    self.assertIn(f'{work}/lvs_checks/trusted:{work}/lvs_checks/trusted:ro', mounts)
                    self.assertFalse(any(m.startswith(f'{work}:') or m.startswith(f'{work}/checks:') for m in mounts))
                else:
                    self.assertIn(f'{work}/checks:{work}/checks:ro', mounts)
                    self.assertIn(f'{work}/lvs_checks:{work}/lvs_checks:ro', mounts)
            self.assertEqual(len(names), len(commands))

    def test_three_schedules_keep_checks_limits_and_branch_isolation(self):
        for schedule in ('serial', 'parallel', 'parallel-lvs'):
            with self.subTest(schedule=schedule):
                self.exercise(schedule)

    def test_mutated_generated_cdl_cannot_pass(self):
        self.exercise('parallel-lvs', tamper_cdl=True)


if __name__ == '__main__':
    unittest.main()
