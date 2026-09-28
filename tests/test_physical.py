"""Negative controls for authoritative physical evidence, independent of EDA install."""
import copy
import hashlib
import zipfile
from unittest.mock import patch
import json
import math
from pathlib import Path
import tempfile
import unittest
from verifier.physical import (CORNERS, CORNER_ZERO_METRICS, ZERO_METRICS,
    PRECHECKS, PhysicalError, check_metrics, check_def, check_precheck_xml,
    check_lvs_log, check_sta_reports, run_physical, prepare_functional_models, output_budget_error, MAX_LOG_BYTES, MAX_GENERATED_BYTES, verify_precheck_wheels, ToolsUnavailable)


def valid_metrics():
    result = {name: 0 for name in ZERO_METRICS}
    result.update({'design__instance__area__stdcell': 123456,
        'design__instance__area__macros': 0, 'design__die__bbox': '0 0 1289.28 710.64'})
    for corner in CORNERS:
        for base in CORNER_ZERO_METRICS:
            result[f'{base}__corner:{corner}'] = 0
        for kind in ('setup', 'hold'):
            result[f'timing__{kind}__ws__corner:{corner}'] = 1
    return result


class PhysicalEvidenceTests(unittest.TestCase):
    def test_valid_complete_metrics(self):
        self.assertEqual(check_metrics(valid_metrics())['stdcell_area_um2'], 123456)

    def test_missing_or_nonfinite_area_cannot_score(self):
        for value in (None, float('nan'), float('inf'), -1, 0, True, '100'):
            metrics = valid_metrics()
            metrics['design__instance__area__stdcell'] = value
            with self.subTest(value=value), self.assertRaises(PhysicalError):
                check_metrics(metrics)

    def test_missing_corner_and_violations_reject(self):
        for key in (*ZERO_METRICS, *(f'{base}__corner:{c}' for c in CORNERS for base in CORNER_ZERO_METRICS)):
            for mutation in ('missing', 'violation'):
                metrics = valid_metrics()
                if mutation == 'missing':
                    del metrics[key]
                else:
                    metrics[key] = 1
                with self.subTest(key=key, mutation=mutation), self.assertRaises(PhysicalError):
                    check_metrics(metrics)

    def test_setup_and_hold_require_real_paths(self):
        for corner in CORNERS:
            for kind in ('setup', 'hold'):
                for value in (None, float('inf'), float('nan'), -.001):
                    metrics = valid_metrics()
                    metrics[f'timing__{kind}__ws__corner:{corner}'] = value
                    with self.subTest(corner=corner,kind=kind,value=value), self.assertRaises(PhysicalError):
                        check_metrics(metrics)

    def test_actual_def_rejects_oversized_geometry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'chip.def'
            path.write_text('UNITS DISTANCE MICRONS 1000 ;\nDIEAREA ( 0 0 ) ( 1289280 710640 ) ;')
            self.assertEqual(check_def(path), [0,0,1289.28,710.64])
            path.write_text('UNITS DISTANCE MICRONS 1000 ;\nDIEAREA ( 0 0 ) ( 1299280 710640 ) ;')
            with self.assertRaises(PhysicalError):
                check_def(path)

    def test_precheck_requires_every_completed_check(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'results.xml'
            entries = [f'<testcase name="{name}"/>' for name in PRECHECKS]
            def write(items): path.write_text('<testsuites><testsuite>'+''.join(items)+'</testsuite></testsuites>')
            write(entries)
            self.assertEqual(set(check_precheck_xml(path)), PRECHECKS)
            for broken in (entries[:-1], entries+entries[:1], entries[1:]+['<testcase name="x"/>'],
                    [entries[0].replace('/>', '><skipped/></testcase>')]+entries[1:]):
                write(broken)
                with self.assertRaises(PhysicalError): check_precheck_xml(path)

    def test_lvs_zero_exit_and_empty_circuit_insufficient(self):
        good = 'Devices to compare: layout=357 schematic=276\nCongratulations! Netlists match.'
        self.assertEqual(check_lvs_log(good), [[357,276]])
        for broken in ('', "ERROR : Netlists don't match", 'Congratulations! Netlists match.',
                       good.replace('layout=357', 'layout=0'), good+"Netlists don't match"):
            with self.assertRaises(PhysicalError): check_lvs_log(broken)

    def test_sta_constraint_audit_rejects_unconstrained_or_missing_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            marker = 'check_setup -verbose -unconstrained_endpoints -multiple_clock -no_clock -no_input_delay -loops -generated_clocks'
            for corner in CORNERS:
                directory = path/corner
                directory.mkdir()
                (directory/'checks.rpt').write_text(marker+'\n====\n')
                for name in ('min.rpt','max.rpt'):
                    (directory/name).write_text('Startpoint: ff\nEndpoint: out\nslack 1')
                (directory/'clock.rpt').write_text('Clock: clk\nPeriod: 20\n')
                (directory/'unpropagated.rpt').write_text('')
            check_sta_reports(path)
            target=path/CORNERS[0]/'checks.rpt'
            for text in ('', marker+'\nWarning: 2 unconstrained endpoints', marker+'\nPath Group: **unconstrained**'):
                target.write_text(text)
                with self.assertRaises(PhysicalError): check_sta_reports(path)
            target.write_text(marker)
            (path/CORNERS[0]/'max.rpt').write_text('No paths found.')
            with self.assertRaises(PhysicalError): check_sta_reports(path)

    def test_missing_tools_cannot_accept_saved_candidate_reports(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            candidate=root/'candidate'
            candidate.mkdir()
            (candidate/'chip.v').write_text('module tt_um_chip(); endmodule')
            (candidate/'candidate.json').write_text(json.dumps({'top_module':'tt_um_chip', 'rtl_files':['chip.v']}))
            (candidate/'physical-result.json').write_text('{"status":"pass","score":1e99}')
            result=run_physical(candidate,root/'output',{'tools_root':str(root/'absent-tools')})
            self.assertEqual(result['status'],'blocked')
            self.assertIsNone(result['gate_netlist'])
            self.assertEqual(result['metrics'],{})

    def test_functional_model_conversion_records_both_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'model.v'
            source.write_text("module cell(input D, output Q); wire delayed_D; assign Q=delayed_D; specify (D=>Q)=1; endspecify endmodule")
            paths = prepare_functional_models([source], root / 'generated')
            metadata = json.loads(Path(paths[0]).with_suffix('.json').read_text())
            self.assertFalse(metadata['timing_simulation'])
            self.assertEqual(metadata['removed_specify_blocks'], 1)
            self.assertEqual(metadata['direct_delayed_signal_aliases'], 1)
            self.assertNotEqual(metadata['sha256_source'], metadata['sha256_functional'])
            self.assertIn('assign delayed_D = D;', Path(paths[0]).read_text())
            with self.assertRaises(PhysicalError):
                prepare_functional_models([source], root / 'generated')

    def test_excessive_logs_or_generated_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            log = root / 'tool.log'
            with log.open('wb') as stream:
                stream.truncate(MAX_LOG_BYTES + 1)
            self.assertIn('log limit', output_budget_error(root))
            log.unlink()
            artifact = root / 'oversized.gds'
            with artifact.open('wb') as stream:
                stream.truncate(MAX_GENERATED_BYTES + 1)
            self.assertIn('generated-output limit', output_budget_error(root))

    def test_precheck_packages_must_match_checksum_pinned_wheels(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'wheels').mkdir()
            packages = root / 'precheck-python'
            packages.mkdir()
            wheel = root / 'wheels/test.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('trusted.py', 'answer = 42')
            (packages / 'trusted.py').write_text('answer = 42')
            lock = {'packages': [{'filename': 'test.whl', 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}]}
            with patch('verifier.physical.WHEEL_LOCK', lock):
                self.assertEqual(verify_precheck_wheels(root), packages)
                (packages / 'trusted.py').write_text('answer = 0')
                with self.assertRaises(ToolsUnavailable): verify_precheck_wheels(root)
                (packages / 'trusted.py').write_text('answer = 42')
                (packages / 'injected.py').write_text('malicious')
                with self.assertRaises(ToolsUnavailable): verify_precheck_wheels(root)
                (packages / 'injected.py').unlink()
                wheel.write_bytes(b'changed archive')
                with self.assertRaises(ToolsUnavailable): verify_precheck_wheels(root)

    def test_previous_workdir_never_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            candidate=root/'candidate'
            candidate.mkdir()
            output=root/'output'
            output.mkdir()
            (output/'state_out.json').write_text('{"pass":true}')
            result=run_physical(candidate,output)
            self.assertEqual(result['status'],'fail')
            self.assertIn('fresh',result['checks'][0]['detail'])


if __name__=='__main__': unittest.main()
