"""Verifier boundary tests. Optional integration checks exercise real EDA/Docker.

Run unit checks with ``python -m unittest discover -s tests``. Set
RUN_VERIFIER_INTEGRATION=1 for the actual synthesizer and compiler isolation.
Functional mutation controls live in scripts/test_mutations.py.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from verifier.candidate import PORTS, check_ports, snapshot, validate
from verifier.common import ROOT, VerificationError, finite_number, read_json, within
from verifier.functional import ContractError, validate_adapter

BASELINE = ROOT / "candidates/tempo"
INTEGRATION = os.environ.get("RUN_VERIFIER_INTEGRATION") == "1"


class CandidateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        # Match the CLI's canonical paths so Docker mounts and HDL filenames
        # agree on macOS, where /var is a symlink to /private/var.
        self.root = Path(self.temporary.name).resolve()
        self.candidate = self.root / "candidate"
        shutil.copytree(BASELINE, self.candidate)

    def manifest(self, **updates):
        path = self.candidate / "candidate.json"
        data = json.loads(path.read_text())
        data.update(updates)
        path.write_text(json.dumps(data))
        return data

    def test_baseline_validate_and_snapshot_preserve_source_identity(self):
        manifest, hashes = validate(self.candidate)
        cloned = snapshot(self.candidate, self.root / "snapshot", hashes)
        self.assertEqual(validate(cloned), (manifest, hashes))
        self.assertIn("candidate.json", hashes)
        self.assertIn(manifest["adapter"], hashes)
        self.assertTrue(set(manifest["rtl_files"]).issubset(hashes))

    def test_simulation_tasks_cannot_forge_verifier_completion(self):
        source = self.candidate / "src/tt_um_protocol_emulator.v"
        original = source.read_text()
        for code in ('initial $finish;', 'always @(posedge clk) $display("VERIFIER_COMPLETE fake");',
                     'always @(posedge clk) $system("true");', '`include "/etc/passwd"'):
            with self.subTest(code=code):
                source.write_text(original + "\n" + code)
                with self.assertRaises(VerificationError):
                    validate(self.candidate)

    def test_symlinked_file_and_directory_cannot_escape_snapshot(self):
        sentinel = self.root / "outside-secret.txt"
        sentinel.write_text("must not become candidate input")
        for name, target in (("secret", sentinel), ("outside", self.root)):
            link = self.candidate / name
            link.symlink_to(target, target_is_directory=target.is_dir())
            try:
                with self.assertRaisesRegex(VerificationError, "symlink"):
                    validate(self.candidate)
                with self.assertRaises(VerificationError):
                    within(self.candidate, name if target.is_file() else name + "/outside-secret.txt")
            finally:
                link.unlink()

    def test_manifest_cannot_address_outside_candidate(self):
        for value in ("../outside.v", "/tmp/outside.v", "src/../../outside.v", ".hidden/chip.v", "src\\chip.v"):
            with self.subTest(value=value):
                self.manifest(rtl_files=[value])
                with self.assertRaises(VerificationError):
                    validate(self.candidate)

    def test_apache_label_requires_complete_license_text(self):
        for fake in ("Apache-2.0", "Licensed under Apache License, Version 2.0.",
                     (self.candidate / "LICENSE").read_text().replace("perpetual", "temporary", 1)):
            with self.subTest(prefix=fake[:40]):
                (self.candidate / "LICENSE").write_text(fake)
                with self.assertRaisesRegex(VerificationError, "License text"):
                    validate(self.candidate)

    def test_changed_manifest_and_source_are_rejected_during_snapshot(self):
        for relative in ("candidate.json", "src/tempo_core.v", "adapter.py"):
            with self.subTest(relative=relative):
                _, hashes = validate(self.candidate)
                path = self.candidate / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                try:
                    with self.assertRaisesRegex(VerificationError, "changed while snapshotting"):
                        snapshot(self.candidate, self.root / ("snapshot-" + path.name), hashes)
                finally:
                    path.write_bytes(original)

    def test_duplicate_json_keys_cannot_override_manifest_fields(self):
        (self.candidate / "candidate.json").write_text('{"schema_version":1,"schema_version":2}')
        with self.assertRaisesRegex(VerificationError, "Duplicate JSON key"):
            validate(self.candidate)

    def test_missing_wrong_width_or_extra_tt_ports_rejected(self):
        correct = {"ports": {name: {"direction": direction, "bits": list(range(width))}
                              for name, (direction, width) in PORTS.items()}}
        check_ports(correct)
        for name in PORTS:
            for mutation in ("missing", "direction", "width"):
                changed = copy.deepcopy(correct)
                if mutation == "missing":
                    del changed["ports"][name]
                elif mutation == "direction":
                    changed["ports"][name]["direction"] = "inout"
                else:
                    changed["ports"][name]["bits"].append(99)
                with self.subTest(name=name, mutation=mutation), self.assertRaises(VerificationError):
                    check_ports(changed)
        correct["ports"]["extra"] = {"direction": "input", "bits": [99]}
        with self.assertRaises(VerificationError):
            check_ports(correct)


class AdapterAndMetricBoundaryTests(unittest.TestCase):
    def valid(self):
        return {"stages": [{"setup": [{"op": "drive", "value": 0, "cycles": 1}],
                            "start": [{"op": "drive", "value": 1, "cycles": 1}],
                            "read": [{"op": "capture", "byte": 0, "src_lsb": 0, "width": 8, "dst_lsb": 0}]}]}

    def test_capture_must_observe_all_result_bits_once(self):
        response = self.valid()
        validate_adapter(response, [1])
        for captures in ([], [dict(response["stages"][0]["read"][0], width=7)],
                         response["stages"][0]["read"] * 2,
                         [{"op": "capture", "byte": 0, "src_lsb": 0, "width": 8, "dst_lsb": 1}]):
            response = self.valid()
            response["stages"][0]["read"] = captures
            with self.subTest(captures=captures), self.assertRaises(ContractError):
                validate_adapter(response, [1])

    def test_adapter_cannot_supply_expected_results_hdl_or_expressions(self):
        for action in ({"op": "capture", "byte": 0, "value": 42},
                       {"op": "drive", "value": "0; $finish;", "cycles": 1},
                       {"op": "drive", "value": True, "cycles": 1},
                       {"op": "drive", "value": 0, "cycles": 1000000000},
                       {"op": "eval", "code": "$finish"}):
            response = self.valid()
            response["stages"][0]["start"] = [action]
            with self.subTest(action=action), self.assertRaises(ContractError):
                validate_adapter(response, [1])
        response = self.valid()
        response["score"] = 1e99
        with self.assertRaises(ContractError):
            validate_adapter(response, [1])

    def test_nonfinite_and_boolean_scores_rejected(self):
        for value in (None, float("nan"), float("inf"), -1, 0, True, "1"):
            with self.subTest(value=value), self.assertRaises(VerificationError):
                finite_number(value, "score", positive=True)


@unittest.skipUnless(INTEGRATION, "set RUN_VERIFIER_INTEGRATION=1 for real synthesis/Docker checks")
class RealBoundaryIntegrationTests(unittest.TestCase):
    setUp = CandidateBoundaryTests.setUp

    def test_synthesizable_design_missing_enable_port_rejected(self):
        from verifier.synthesis import run_synthesis
        source = self.candidate / "src/tt_um_protocol_emulator.v"
        text = source.read_text()
        self.assertIn("    input wire ena,\n", text)
        text = text.replace("    input wire ena,\n", "", 1).replace(");", ");\n    wire ena = 1'b1;", 1)
        source.write_text(text)
        manifest, _ = validate(self.candidate)
        with self.assertRaisesRegex(VerificationError, "port set"):
            run_synthesis(self.candidate, manifest, self.root / "synthesis")

    def test_adapter_cannot_read_host_write_candidate_or_use_network(self):
        from verifier.isolation import compiler
        sentinel = self.root / "private-sentinel.txt"
        sentinel.write_text("PRIVATE-HOST-SENTINEL")
        adapter = self.candidate / "adapter.py"
        adapter.write_text('''import json, pathlib, socket
result = {}
def denied(name, operation):
    try:
        operation()
        result[name] = False
    except (OSError, PermissionError):
        result[name] = True
denied("host_read", lambda: pathlib.Path(''' + repr(str(sentinel)) + ''').read_text())
denied("candidate_write", lambda: pathlib.Path("/candidate/LICENSE").write_text("changed"))
denied("judge_write", lambda: pathlib.Path("/verifier/injected").write_text("changed"))
denied("docker_socket", lambda: pathlib.Path("/var/run/docker.sock").stat())
def connect():
    sock=socket.socket(); sock.settimeout(1)
    try: sock.connect(("1.1.1.1",443))
    finally: sock.close()
denied("network", connect)
print(json.dumps(result))
''')
        manifest, hashes = validate(self.candidate)
        response = compiler(self.candidate, manifest, self.root / "compiler")({"kind": "boundary-test"})
        self.assertEqual(response, {name: True for name in ("host_read", "candidate_write", "judge_write", "docker_socket", "network")})
        self.assertEqual(sentinel.read_text(), "PRIVATE-HOST-SENTINEL")
        self.assertEqual(validate(self.candidate)[1], hashes)

    def test_nonterminating_adapter_leaves_no_container(self):
        from verifier.isolation import compiler
        adapter = self.candidate / "adapter.py"
        adapter.write_text("while True:\n    pass\n")
        manifest, _ = validate(self.candidate)
        suffix = "integration" + secrets.token_hex(8)
        with patch("verifier.isolation.secrets.token_hex", return_value=suffix):
            with self.assertRaisesRegex(VerificationError, "compiler (failed|timed out)"):
                compiler(self.candidate, manifest, self.root / "compiler")({"kind": "timeout-test"})
        inspected = subprocess.run(["docker", "inspect", "protocol-compiler-" + suffix],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        self.assertNotEqual(inspected.returncode, 0)
        self.assertRegex(inspected.stderr.lower(), "no such (object|container)")


if __name__ == "__main__":
    unittest.main()
