"""Acceptance boundaries that must hold even during concurrent agent edits."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from verifier import candidate as candidate_module
from verifier.cli import evaluate
from verifier.common import ROOT


class AcceptanceBoundaries(unittest.TestCase):
    def test_checked_bytes_are_snapshot_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'candidate'
            shutil.copytree(ROOT / 'candidates/tempo', source)
            injected = False
            original = candidate_module.digest

            def change_between_validation_and_hash(path):
                nonlocal injected
                if not injected:
                    injected = True
                    rtl = source / 'src/tempo_core.v'
                    rtl.write_text(rtl.read_text() + '\ninitial $finish;\n')
                return original(path)

            with patch.object(candidate_module, 'digest', change_between_validation_and_hash):
                result = evaluate(source, root / 'evaluation')
            self.assertEqual(result['status'], 'fail')
            self.assertFalse(result['accepted'])
            self.assertIsNone(result['score'])
            self.assertIn('simulation', result['error'].lower())
            self.assertEqual(result['stages'], {})

    def test_existing_evidence_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / 'evaluation'
            evidence.mkdir()
            sentinel = evidence / 'result.json'
            sentinel.write_text('existing evidence')
            with self.assertRaises(FileExistsError):
                evaluate(ROOT / 'candidates/tempo', evidence)
            self.assertEqual(sentinel.read_text(), 'existing evidence')

    def test_source_is_never_a_run_destination(self):
        with self.assertRaisesRegex(Exception, 'outside the candidate'):
            evaluate(ROOT / 'candidates/tempo', ROOT / 'candidates/tempo/output')


if __name__ == '__main__':
    unittest.main()
