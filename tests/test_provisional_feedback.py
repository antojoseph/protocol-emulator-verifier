import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from verifier.candidate import validate
from verifier.cli import harness_hashes
from verifier.common import digest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('provisional_feedback', ROOT / 'scripts/provisional_feedback.py')
feedback = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(feedback)


class ProvisionalFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.candidate = self.root / 'candidate'
        self.evaluation = self.root / 'evaluation'
        shutil.copytree(ROOT / 'candidates/tempo', self.candidate)
        shutil.copytree(self.candidate, self.evaluation / 'candidate')
        self.record = {
            'mode': 'full', 'status': 'running', 'accepted': False, 'score': None,
            'candidate': {'source_sha256': validate(self.candidate)[1]},
            'harness_sha256': harness_hashes(),
            'stages': {
                'synthesis': {'status': 'pass', 'metrics': {'generic_cells': 100}},
                'functional': {'status': 'pass', 'metrics': {'case_count': 43, 'stage_count': 75, 'assertions': 1000}},
            },
        }
        self.save()
        self.run = self.evaluation / 'physical/runs/authoritative'
        self.run.mkdir(parents=True)

    def save(self):
        (self.evaluation / 'result.json').write_text(json.dumps(self.record))

    def stages(self, count=44):
        for i, suffix in enumerate(feedback.EARLY_STAGES[:count], 1):
            stage = self.run / f'{i:02d}-{suffix}'
            stage.mkdir()
            (stage / 'runtime.txt').write_text('00:00:01.000')
            # Inherited state metrics must never leak into direct observations.
            (stage / 'state_out.json').write_text(json.dumps({'metrics': {
                'timing__setup__ws__corner:nom_fast_1p32V_m40C': -999,
                'design__instance__area__stdcell': 777777,
            }}))
            direct = {}
            if i == 28:
                direct['design__instance__area__stdcell'] = 100
            if i == 38:
                direct['timing__setup__ws__corner:nom_fast_1p32V_m40C'] = -1
            if i == 41:
                direct['design__instance__area__stdcell'] = 150
            if i == 44:
                direct['timing__setup__ws__corner:nom_typ_1p20V_25C'] = 8
            if direct:
                (stage / 'or_metrics_out.json').write_text(json.dumps(direct))

    def observe(self):
        result = feedback.observe(self.evaluation, self.candidate)
        self.assertIs(result['accepted'], False)
        self.assertIsNone(result['score'])
        return result

    def test_complete_feedback_keeps_per_metric_provenance_and_omits_stale_corners(self):
        self.stages()
        result = self.observe()
        self.assertEqual(result['status'], 'feedback_available')
        self.assertTrue(result['early_target_complete'])
        self.assertEqual(len(result['completed_early_stages']), 44)
        self.assertEqual(result['observed_tool_runtime_seconds'], 44)
        area = result['metrics']['design__instance__area__stdcell']
        self.assertEqual((area['value'], area['stage']), (150, '41-openroad-repairdesignpostgrt'))
        self.assertNotIn('timing__setup__ws__corner:nom_fast_1p32V_m40C', result['metrics'])
        self.assertEqual(result['metrics']['timing__setup__ws__corner:nom_typ_1p20V_25C']['value'], 8)

    def test_no_completed_stages_is_pending(self):
        result = self.observe()
        self.assertEqual(result['status'], 'pending')
        self.assertFalse(result['early_target_complete'])

    def test_partial_marker_ignores_its_metrics_and_later_stages(self):
        self.stages()
        (self.run / '39-openroad-globalrouting/runtime.txt').write_text('00:00:')
        result = self.observe()
        self.assertEqual(len(result['completed_early_stages']), 38)
        self.assertEqual(result['metrics']['design__instance__area__stdcell']['value'], 100)
        self.assertEqual(result['next_incomplete_early_stage'], '39-openroad-globalrouting')

    def test_missing_marker_stops_prefix_even_with_finished_looking_files(self):
        self.stages()
        (self.run / '39-openroad-globalrouting/runtime.txt').unlink()
        self.assertEqual(len(self.observe()['completed_early_stages']), 38)

    def test_duplicate_stage_is_rejected(self):
        self.stages(1)
        (self.run / '01-other').mkdir()
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_marker_without_valid_output_state_is_rejected(self):
        self.stages(1)
        (self.run / '01-verilator-lint/state_out.json').write_text('{')
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_symlink_stage_and_metric_are_rejected(self):
        self.stages(28)
        stage = self.run / '28-openroad-globalplacement'
        original = stage / 'or_metrics_out.json'
        original.unlink()
        target = self.root / 'external.json'
        target.write_text('{}')
        original.symlink_to(target)
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')
        original.unlink()
        moved = self.root / stage.name
        stage.rename(moved)
        stage.symlink_to(moved, target_is_directory=True)
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_changed_firmware_or_rtl_invalidates_observation_identity(self):
        self.stages(1)
        for name in ('adapter.py', 'src/tempo_core.v'):
            with self.subTest(name=name):
                p = self.candidate / name
                before = p.read_bytes()
                p.write_bytes(before + b'\n')
                self.assertEqual(self.observe()['status'], 'feedback_unavailable')
                p.write_bytes(before)

    def test_changed_snapshot_or_harness_is_rejected(self):
        self.stages(1)
        p = self.evaluation / 'candidate/NOTICE'
        original = p.read_bytes()
        p.write_bytes(original + b'changed')
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')
        p.write_bytes(original)
        self.record['harness_sha256']['verifier/physical.py'] = '0' * 64
        self.save()
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_failed_blocked_or_incomplete_functional_runs_never_produce_feedback(self):
        self.stages(1)
        for status in ('fail', 'blocked', 'unknown'):
            self.record['status'] = status
            self.save()
            self.assertEqual(self.observe()['status'], 'feedback_unavailable')
        self.record['status'] = 'running'
        self.record['stages']['functional']['status'] = 'fail'
        self.save()
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_previous_acceptance_is_never_reissued(self):
        self.stages(1)
        self.record.update(status='pass', accepted=True, score=2)
        self.save()
        result = self.observe()
        self.assertEqual(result['status'], 'feedback_available')
        from service.core import classify
        with self.assertRaises(ValueError):
            classify(result, 'full', 0, self.record['candidate']['source_sha256'], 1)

    def test_nonfinite_and_oversized_metrics_are_rejected(self):
        self.stages(28)
        p = self.run / '28-openroad-globalplacement/or_metrics_out.json'
        for content in ('{"x":NaN}',
                        '{"design__instance__area__stdcell":1e999}',
                        ' ' * (4 * 1024 * 1024 + 1)):
            p.write_text(content)
            self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_duplicate_direct_metrics_are_omitted_even_when_equal(self):
        self.stages(28)
        p = self.run / '28-openroad-globalplacement/or_metrics_out.json'
        key = 'design__instance__area__stdcell'
        for values in ((1, 2, 3), (2, 2, 2)):
            p.write_text('{' + ','.join(json.dumps(key) + ':' + str(v) for v in values) + '}')
            result = self.observe()
            self.assertEqual(result['status'], 'feedback_available')
            self.assertNotIn(key, result['metrics'])
            self.assertEqual(result['omitted_duplicate_metric_keys']['28-openroad-globalplacement'], [key])

    def test_duplicate_source_record_keys_are_rejected(self):
        p = self.evaluation / 'result.json'
        p.write_text('{"mode":"full","mode":"fast"}')
        self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_concurrent_completed_metric_mutation_is_rejected(self):
        self.stages(28)
        p = self.run / '28-openroad-globalplacement/or_metrics_out.json'
        original = feedback.Evidence.recheck
        def mutate(reader):
            p.write_text('{"design__instance__area__stdcell":1}')
            original(reader)
        with patch.object(feedback.Evidence, 'recheck', mutate):
            self.assertEqual(self.observe()['status'], 'feedback_unavailable')

    def test_read_only_and_never_reads_private_or_netlist_artifacts(self):
        self.stages()
        paths = [p for p in self.evaluation.rglob('*') if p.is_file()]
        before = {p: digest(p) for p in paths}
        accessed = []
        original = feedback.Evidence.read
        def read(reader, path, *args):
            accessed.append(Path(path).name)
            return original(reader, path, *args)
        with patch.object(feedback.Evidence, 'read', read):
            self.assertEqual(self.observe()['status'], 'feedback_available')
        self.assertEqual(before, {p: digest(p) for p in paths})
        self.assertLessEqual(set(accessed), {'result.json', 'runtime.txt', 'state_out.json', 'or_metrics_out.json'})


if __name__ == '__main__':
    unittest.main()
