"""Check the public entrypoint selects the exact validated configuration."""
import contextlib
import io
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import patch

import verifier.cli


class DefaultScheduleTests(unittest.TestCase):
    def test_default_and_explicit_overrides_use_real_parser(self):
        entrypoint = Path(__file__).resolve().parents[1] / 'verify'
        for options, expected in [([], 'parallel-lvs'),
                                  (['--physical-schedule', 'parallel'], 'parallel'),
                                  (['--physical-schedule=serial'], 'serial')]:
            with self.subTest(options=options), patch.object(sys, 'argv',
                    [str(entrypoint), 'candidates/tempo', '--mode', 'full', *options]), \
                    patch.object(verifier.cli, 'evaluate', return_value={
                        'status': 'pass', 'accepted': True, 'score': 1.0}) as evaluate, \
                    contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exited:
                    runpy.run_path(str(entrypoint), run_name='__main__')
                self.assertEqual(exited.exception.code, 0)
                self.assertEqual(evaluate.call_args.kwargs['physical_schedule'], expected)
                self.assertEqual(evaluate.call_args.kwargs['mode'], 'full')
