#!/usr/bin/env python3
"""Run real fast verification against baseline and synthesizable broken designs.

This is a verifier test, not candidate acceptance. Every mutant must pass source
validation and synthesis, then fail a trusted external-pin assertion. A missing
tool, compiler failure or simulation compile failure never counts as rejection.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import secrets
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verifier.cli import evaluate
from verifier.common import write_json

# These are fixture mutations of Tempo's behavior, not ISA requirements.
# Any replacement candidate remains free to change every internal implementation.
PAD_MUTATIONS = {
    "uart_inverted_data": ("({original}) ^ 8'h01", r"FAIL.*UART"),
    "spi_inverted_mosi": ("({original}) ^ (uio_oe[3] ? 8'h01 : 8'h00)", r"FAIL.*SPI"),
    "i2c_drives_high": ("({original}) | ((!uio_oe[3] && uio_oe[1]) ? 8'h03 : 8'h00)", r"FAIL.*I2C"),
    "programmable_inverted_output": ("({original}) ^ 8'h80", r"FAIL.*programmable"),
}
SOURCE_MUTATIONS = {
    "ignore_programmable_wait": {
        "file": "src/tempo_core.v", "count": 1,
        "old": "wire stalled = ((opcode == 4'h5) &&",
        "new": "wire stalled = ((divider != 16'd0) && (opcode == 4'h5) &&",
        "failure": r"FAIL.*(?:programmable|case=11 bounded per-case watchdog)",
    },
    "wrong_host_capture": {
        "file": "adapter.py", "count": 2,
        "old": '"src_lsb": 0', "new": '"src_lsb": 1',
        "failure": r"FAIL.*host read equals independent peer data/result",
    },
    "premature_programmable_pulse": {
        "file": "adapter.py", "count": 1,
        "old": "SET dirs, 128\nSET x,",
        "new": "SET dirs, 128\nSET setpins, 128 @ 4\nSET clearpins, 128\nSET x,",
        "failure": r"FAIL unsolicited programmable output outside the triggered pulse train",
    },
    "hardcoded_branch_guess": {
        "file": "adapter.py",
        "patches": [
            ("IN left1\nJMP zero, false_branch", "JMP always, false_branch", 1),
            ('programs = [(generic_program(program), 128, 0, 0, [], 0) for program in request["programs"]]',
             'programs = [(generic_program(dict(program, width_false=program["width_false" if index == 0 else "width_true"])), 128, 0, 0, [], 0) for index, program in enumerate(request["programs"])]', 1),
        ],
        "failure": r"FAIL.*runtime branch selected programmed pulse width",
    },
}
MUTATIONS = {**{name: value[1] for name, value in PAD_MUTATIONS.items()},
             **{name: value["failure"] for name, value in SOURCE_MUTATIONS.items()}}


def mutate(candidate: Path, name: str):
    if name in SOURCE_MUTATIONS:
        mutation = SOURCE_MUTATIONS[name]
        path = candidate / mutation["file"]
        source = path.read_text()
        patches = mutation.get("patches") or [(mutation["old"], mutation["new"], mutation["count"])]
        for old, new, count in patches:
            if source.count(old) != count:
                raise AssertionError("Tempo fixture changed; update mutation anchor: " + name)
            source = source.replace(old, new)
        path.write_text(source)
        return
    expression = PAD_MUTATIONS[name][0]
    path = candidate / "src/tt_um_protocol_emulator.v"
    source = path.read_text()
    pattern = r"assign\s+uio_out\s*=\s*(.*?);"
    matched = re.search(pattern, source, flags=re.S)
    if matched is None or len(re.findall(pattern, source, flags=re.S)) != 1:
        raise AssertionError("Tempo fixture output assignment changed; update mutation anchors")
    replacement = "assign uio_out = " + expression.format(original=matched.group(1)) + ";"
    path.write_text(source[:matched.start()] + replacement + source[matched.end():])


def require_baseline(result):
    if result["status"] != "pass" or result.get("accepted") or result.get("score") is not None:
        raise AssertionError("baseline must pass fast mode with no full-acceptance score: " + str(result.get("error")))
    if not result.get("provisional_score", 0) > 0:
        raise AssertionError("baseline did not produce a positive provisional synthesis score")


def require_functional_rejection(result, expected_message):
    if result["status"] != "fail" or result.get("accepted") or result.get("score") is not None:
        raise AssertionError("mutant was accepted or merely infrastructure-blocked")
    if result.get("stages", {}).get("synthesis", {}).get("status") != "pass":
        raise AssertionError("mutant must remain synthesizable")
    functional = result.get("stages", {}).get("functional", {})
    checks = {check["name"]: check["status"] for check in functional.get("checks", [])}
    if checks.get("adapter_contract") != "pass" or checks.get("compile") != "pass":
        raise AssertionError("adapter/compile failure does not establish a functional negative control")
    if checks.get("external_pin_protocols") != "fail":
        raise AssertionError("mutant did not fail external pin behavior")
    log_path = functional.get("artifacts", {}).get("simulation_log")
    log = Path(log_path).read_text() if log_path else ""
    if not re.search(expected_message, log):
        raise AssertionError("mutant failed for an unexpected reason; inspect simulation log")
    return [line for line in log.splitlines() if re.search(expected_message, line)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--seeds", type=int, nargs="+", default=[20260928, 8675309])
    args = parser.parse_args()
    output = (args.out or ROOT / ".runs" / ("mutations-" + secrets.token_hex(5))).resolve()
    output.mkdir(parents=True, exist_ok=False)
    records = []
    report = {"status": "running", "started_utc": datetime.now(timezone.utc).isoformat(),
              "seeds": args.seeds, "controls": records}
    write_json(output / "mutation-results.json", report)
    try:
        for seed in args.seeds:
            print(f"BASELINE seed={seed}", flush=True)
            baseline = evaluate(ROOT / "candidates/tempo", output / f"baseline-{seed}", mode="fast", seed=seed)
            require_baseline(baseline)
            records.append({"name": "baseline", "seed": seed, "status": "pass"})
            write_json(output / "mutation-results.json", report)
            for name, failure in MUTATIONS.items():
                print(f"NEGATIVE CONTROL {name} seed={seed}", flush=True)
                candidate = output / f"candidate-{name}-{seed}"
                shutil.copytree(ROOT / "candidates/tempo", candidate)
                mutate(candidate, name)
                result = evaluate(candidate, output / f"result-{name}-{seed}", mode="fast", seed=seed)
                observed_failures = require_functional_rejection(result, failure)
                records.append({"name": name, "seed": seed, "status": "rejected-as-expected",
                                "failed_stage": "functional", "error": result.get("error"),
                                "assertion_failures": observed_failures})
                write_json(output / "mutation-results.json", report)
        report["status"] = "pass"
    except Exception as error:
        report.update(status="fail", error=str(error))
        raise
    finally:
        report["completed_utc"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "mutation-results.json", report)
    print(f"PASS: {len(args.seeds)} baseline runs and {len(args.seeds)*len(MUTATIONS)} functional mutation rejections")
    print(output / "mutation-results.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
