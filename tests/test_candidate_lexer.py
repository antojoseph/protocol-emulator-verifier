"""Regression controls for strings/escaped identifiers and hierarchy work bounds."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from verifier.candidate import PORTS, validate, verilog_code_for_checks
from verifier.common import ROOT, VerificationError
from verifier.synthesis import run_synthesis


class CandidateLexerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.candidate = Path(self.temporary.name)/'candidate'
        shutil.copytree(ROOT/'candidates/tempo', self.candidate)
        self.path = self.candidate/'src/tt_um_protocol_emulator.v'
        self.original = self.path.read_text()

    def insert(self, code):
        self.path.write_text(self.original.replace('endmodule', code+'\nendmodule', 1))

    def test_unchanged_baseline_remains_valid(self):
        validate(self.candidate)

    def test_string_comment_marker_cannot_hide_conditional_simulation_code(self):
        self.insert('''
localparam [15:0] probe = "/*";
`ifndef SYNTHESIS
initial $display("forged completion");
`endif
// */
''')
        with self.assertRaises(VerificationError):
            validate(self.candidate)

    def test_escaped_identifier_comment_marker_cannot_hide_system_tasks(self):
        self.insert('''
wire \\/* ;
always @(posedge clk) $display("forged completion");
// */
''')
        with self.assertRaises(VerificationError):
            validate(self.candidate)

    def test_escaped_quote_and_line_comment_marker_cannot_hide_code(self):
        for literal in ('"//"', '"escaped \\\" /*"', '"backslash \\\\"'):
            with self.subTest(literal=literal):
                self.insert('localparam [255:0] probe='+literal+';\ninitial $finish;\n// */')
                with self.assertRaises(VerificationError):
                    validate(self.candidate)

    def test_real_comments_and_string_contents_are_not_executable(self):
        source = 'wire good; /* initial $finish; */\nlocalparam p="// initial \\\" /*"; // $fopen\nwire next;'
        audited = verilog_code_for_checks(source)
        self.assertIn('wire good;', audited)
        self.assertIn('wire next;', audited)
        self.assertNotIn('$finish', audited)
        self.assertNotIn('$fopen', audited)
        self.assertNotIn('initial', audited)
        self.assertEqual(audited.count('\n'), source.count('\n'))

    def test_comments_do_not_join_tokens_into_new_spellings(self):
        self.assertEqual(verilog_code_for_checks('foo/**/bar'), 'foo    bar')

    def test_unterminated_strings_and_comments_fail_closed(self):
        for source in ('/* no end', '"no end', '"trailing \\'):
            with self.subTest(source=source), self.assertRaises(VerificationError):
                verilog_code_for_checks(source)

    def test_escaped_special_identifiers_do_not_bypass_restrictions(self):
        for code in ('wire \\$display ;', '(* \\blackbox = 1 *) wire x;',
                     'wire \\/* ;\n`include "untrusted.v"\n// */'):
            with self.subTest(code=code):
                self.insert(code)
                with self.assertRaises(VerificationError):
                    validate(self.candidate)


class SynthesisHierarchyWorkTests(unittest.TestCase):
    def evaluate_modules(self, modules):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)/'synthesis'
            top = 'tt_um_graph'
            modules[top]['ports'] = {name: {'direction': direction, 'bits': list(range(width))}
                                      for name, (direction, width) in PORTS.items()}
            def fake_eda(*args, **kwargs):
                (output/'generic.json').write_text('{}')
                return True, 'exit 0'
            with patch('verifier.synthesis.run_eda', side_effect=fake_eda), \
                 patch('verifier.synthesis.read_json', return_value={'modules': modules}):
                return run_synthesis(Path(temporary), {'top_module': top, 'rtl_files': ['chip.v']}, output)

    def test_shared_hierarchy_dag_counts_instances_with_linear_traversal(self):
        visits = {}
        class CountedModule(dict):
            def get(self, key, default=None):
                if key == 'cells':
                    visits[self['label']] = visits.get(self['label'], 0)+1
                return super().get(key, default)
        modules = {'leaf': CountedModule(label='leaf', cells={'bit': {'type': '$_NOT_'}})}
        previous = 'leaf'
        depth = 40
        for index in range(depth):
            name = 'tt_um_graph' if index == depth-1 else f'level_{index}'
            modules[name] = CountedModule(label=name, cells={
                'left': {'type': previous}, 'right': {'type': previous}})
            previous = name
        result = self.evaluate_modules(modules)
        self.assertEqual(result['metrics']['generic_cells'], 2**depth)
        self.assertEqual(sum(visits.values()), depth+1)
        self.assertTrue(all(count == 1 for count in visits.values()))

    def test_memoization_preserves_recursive_hierarchy_rejection(self):
        modules = {'tt_um_graph': {'cells': {'self': {'type': 'tt_um_graph'}}}}
        with self.assertRaisesRegex(VerificationError, 'Recursive hardware hierarchy'):
            self.evaluate_modules(modules)


if __name__ == '__main__':
    unittest.main()
