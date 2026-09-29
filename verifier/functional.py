"""Trusted functional contract and external-pin verification.

Candidate adapters translate PUBLIC workloads into bounded pin transactions. They
never receive peer response bytes, runtime branch choices or simulation handles.
Only organizer code generates assertions and completion records. This is the
fast digital stage; it is not physical acceptance or an analog certification.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import random
import re
import secrets
from typing import Callable

from .common import ToolUnavailable
from .containers import run_eda


PROGRAMMABLE_CASES = 32


class ContractError(ValueError):
    """A candidate's adapter failed the bounded host interface contract."""


def _integer(value, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ContractError(f"{label} must be an integer in {low}..{high}")
    return value


def validate_adapter(response: dict, read_counts: list[int]) -> list[dict]:
    """Validate every byte before any candidate value enters generated HDL.

    No expressions, arbitrary signal paths, candidate HDL, expected values, or
    delays outside the resource budget are accepted. Capture is observational:
    every requested RX bit must come from a uniquely assigned uo_out bit.
    """
    if not isinstance(response, dict) or set(response) != {"stages"}:
        raise ContractError("adapter response must contain only stages")
    stages = response["stages"]
    if not isinstance(stages, list) or len(stages) != len(read_counts):
        raise ContractError("adapter returned the wrong number of stages")
    total_actions = total_budget = 0
    for stage, count in zip(stages, read_counts):
        if not isinstance(stage, dict) or set(stage) != {"setup", "start", "read"}:
            raise ContractError("each stage requires exactly setup, start and read")
        coverage = [0] * count
        if not stage["setup"] or not stage["start"]:
            raise ContractError("setup and start must have host actions")
        for phase in ("setup", "start", "read"):
            actions = stage[phase]
            if not isinstance(actions, list):
                raise ContractError("host phases must be arrays")
            for action in actions:
                total_actions += 1
                if not isinstance(action, dict):
                    raise ContractError("each host action must be an object")
                op = action.get("op")
                if op == "drive":
                    if set(action) != {"op", "value", "cycles"}:
                        raise ContractError("drive accepts value and cycles only")
                    _integer(action["value"], 0, 255, "drive.value")
                    total_budget += _integer(action["cycles"], 1, 128, "drive.cycles") + 1
                elif op == "wait":
                    if set(action) != {"op", "mask", "value", "timeout"}:
                        raise ContractError("wait accepts mask, value and timeout only")
                    mask = _integer(action["mask"], 1, 255, "wait.mask")
                    value = _integer(action["value"], 0, 255, "wait.value")
                    if value & ~mask:
                        raise ContractError("wait.value has unmasked bits")
                    total_budget += _integer(action["timeout"], 1, 128, "wait.timeout")
                elif op == "capture":
                    if phase != "read":
                        raise ContractError("capture is allowed only in the read phase")
                    if set(action) != {"op", "byte", "src_lsb", "width", "dst_lsb"}:
                        raise ContractError("capture accepts byte, src_lsb, width, dst_lsb only")
                    byte = _integer(action["byte"], 0, count-1, "capture.byte")
                    src = _integer(action["src_lsb"], 0, 7, "capture.src_lsb")
                    dst = _integer(action["dst_lsb"], 0, 7, "capture.dst_lsb")
                    width = _integer(action["width"], 1, 8, "capture.width")
                    if src+width > 8 or dst+width > 8:
                        raise ContractError("capture extends past a byte")
                    bits = ((1 << width)-1) << dst
                    if coverage[byte] & bits:
                        raise ContractError("capture destination bits overlap")
                    coverage[byte] |= bits
                else:
                    raise ContractError("unknown host action")
        if coverage != [255]*count:
            raise ContractError("read phase must capture exactly all eight bits of each result")
    if total_actions > 20000 or total_budget > 1500000:
        raise ContractError("host action resource budget exceeded")
    return stages


def _public_workloads(seed: int) -> list[dict]:
    rng = random.Random(seed)
    byte = lambda: rng.randrange(256)
    cases = []
    for index in range(2):
        cases.append({"kind": "uart_tx", "tx": [0x55 if index == 0 else 0, byte(), byte()],
                      "bit_cycles": 432})
        cases.append({"kind": "uart_rx", "count": 3, "bit_cycles": 432})
        cases.append({"kind": "spi", "tx": [byte(), byte(), byte()], "period_cycles": 36})
    for nack_index in (-1, 0, 1):
        cases.append({"kind": "i2c_write", "tx": [rng.randrange(8, 120) << 1, byte(), byte()],
                      "nack_index": nack_index, "bus_hz": 100000})
    for nack in (False, True):
        cases.append({"kind": "i2c_read", "address": rng.randrange(8, 120), "count": 2,
                      "address_nack": nack, "bus_hz": 100000})
    for _ in range(PROGRAMMABLE_CASES):
        programs = []
        for __ in range(2):
            programs.append({"count": rng.randint(2, 5), "width_false": rng.randint(10, 25),
                             "width_true": rng.randint(30, 50), "gap": rng.randint(8, 24)})
        cases.append({"kind": "programmable", "programs": programs})
    return cases


def _read_counts(request: dict) -> list[int]:
    kind = request["kind"]
    if kind == "uart_tx":
        return [0]
    if kind == "uart_rx":
        return [request["count"]]
    if kind == "spi":
        return [len(request["tx"])]
    if kind == "i2c_write":
        return [len(request["tx"])]
    if kind == "i2c_read":
        return [request["count"]+1]
    if kind == "programmable":
        return [0]*len(request["programs"])
    raise ContractError("unknown workload kind")


def _host_task(name: str, actions: list[dict]) -> str:
    lines = [f"    task {name}; begin"]
    for action in actions:
        if action["op"] == "drive":
            lines.append(f"        host_drive(8'd{action['value']}, {action['cycles']});")
        elif action["op"] == "wait":
            lines.append(f"        host_wait(8'd{action['mask']}, 8'd{action['value']}, {action['timeout']});")
        else:
            lines.append(f"        host_capture({action['byte']}, {action['src_lsb']}, "
                         f"{action['width']}, {action['dst_lsb']});")
    lines.append("    end endtask")
    return "\n".join(lines)


def _scenario(index: int, request: dict, stages: list[dict], rng: random.Random) -> tuple[str, list[str]]:
    """Generate trusted scenarios; random peer values are generated AFTER adapter execution."""
    lines = [f"        case_number={index}; case_started=cycles; reset_chip;"]
    tasks = []
    kind = request["kind"]
    for stage_no, stage in enumerate(stages):
        prefix = f"case_{index}_{stage_no}"
        for phase in ("setup", "start", "read"):
            tasks.append(_host_task(f"{prefix}_{phase}", stage[phase]))
        # Inputs must be stable before loading; peer outputs are never in adapter control.
        lines.append("        input_only=0; peer_enable=0; peer_value=255; i2c_monitor=0;")
        if kind == "uart_rx":
            lines.append("        input_only=1; peer_enable=1; peer_value=255;")
        elif kind == "spi":
            lines.append("        input_only=4;")
        elif kind == "programmable":
            lines.append("        input_only=3; peer_enable=3; peer_value=0;")
        lines.extend(["        programmable_quiet=0;", f"        {prefix}_setup;", "        stage_started=cycles;"])
        expected = []
        useful_bits = 0
        if kind == "uart_tx":
            lines.append(f"        {prefix}_start;")
            for tx in request["tx"]:
                lines.append(f"        uart_receive(8'd{tx}, {request['bit_cycles']});")
            useful_bits = len(request["tx"])*8
        elif kind == "uart_rx":
            expected = [rng.randrange(256) for _ in range(request["count"])]
            # Both phase and +/-0.45% baud offset are independent of the candidate adapter.
            period_ns = request["bit_cycles"]*20+rng.choice((-37, -13, 17, 39))
            lines.extend([f"        {prefix}_start;", "        repeat (256) @(negedge clk);",
                          f"        #{rng.randint(1, 19)};"])
            for value in expected:
                lines.append(f"        uart_send(8'd{value}, {period_ns});")
            lines.append("        repeat (256) @(negedge clk);")
            useful_bits = len(expected)*8
        elif kind == "spi":
            expected = [rng.randrange(256) for _ in request["tx"]]
            lines.append(f"        {prefix}_start;")
            for tx, rx in zip(request["tx"], expected):
                lines.append(f"        spi_peer(8'd{tx}, 8'd{rx}, {request['period_cycles']});")
            useful_bits = len(expected)*16
        elif kind in ("i2c_write", "i2c_read"):
            lines.extend(["        i2c_starts=0; i2c_stops=0; i2c_rises=0;",
                          "        i2c_previous_rise=0; i2c_start_at=cycles; i2c_sda_at=cycles;",
                          "        i2c_high_at=cycles-1000; i2c_low_at=cycles-1000; i2c_monitor=1;",
                          f"        {prefix}_start; i2c_start;"])
            stretch = rng.randint(1500, 2400)
            if kind == "i2c_write":
                tx = request["tx"]
                nack_index = request["nack_index"]
                actual = tx if nack_index < 0 else tx[:nack_index+1]
                for n, value in enumerate(actual):
                    nack = int(n==nack_index)
                    expected.append(nack)
                    lines.append(f"            i2c_receive(8'd{value}, {nack}, {stretch if n==0 else 0});")
                rises = len(actual)*9+1
                useful_bits = len(actual)*8
            else:
                nack = int(request["address_nack"])
                expected = [nack]
                lines.append(f"            i2c_receive(8'd{request['address']*2+1}, {nack}, {stretch});")
                if not nack:
                    for n in range(request["count"]):
                        value = rng.randrange(256)
                        expected.append(value)
                        lines.append(f"            i2c_send(8'd{value}, {int(n==request['count']-1)});")
                rises = len(expected)*9+1
                useful_bits = len(expected)*8
            lines.extend(["            i2c_stop;",
                          "        repeat (240) @(negedge clk);",
                          f"        expect_true(i2c_starts==1 && i2c_stops==1 && i2c_rises=={rises},",
                          "                    \"I2C transaction has exact START/STOP and byte clock counts\");",
                          "        i2c_monitor=0;"])
        elif kind == "programmable":
            program = request["programs"][stage_no]
            if stage_no == 0:
                first_branch = rng.randrange(2)
            branch = first_branch if stage_no == 0 else 1-first_branch
            width = program["width_true" if branch else "width_false"]
            lines.extend([f"        {prefix}_start;", "        arm_programmable;",
                          f"        peer_value[1]={branch}; repeat ({rng.randint(8, 30)}) @(negedge clk);",
                          "        trigger_at=cycles;",
                          "        fork begin @(negedge clk); programmable_quiet=0; peer_value[0]=1; end",
                          f"             programmable_pulses({program['count']}, {width}, {program['gap']}, trigger_at); join"])
            useful_bits = program["count"]
        lines.append("        stage_ended=cycles;")
        lines.append("        repeat (256) @(negedge clk);")
        for n in range(len(expected)):
            lines.append(f"        captured[{n}]=0;")
        lines.append(f"        {prefix}_read;")
        for n, value in enumerate(expected):
            lines.append(f"        expect_true(captured[{n}] === 8'd{value}, \"host read equals independent peer data/result\");")
        lines.append(f'        $display("STAGE_COMPLETE case={index} stage={stage_no} kind={kind} bits={useful_bits} cycles=%0d", stage_ended-stage_started);')
    return "\n".join(lines), tasks


def run_functional(candidate_dir, manifest, output_dir, seed=20260928,
                   adapter_runner: Callable[[dict], dict] | None = None,
                   gate_netlist=None, pdk_models=None, peer_seed=None) -> dict:
    """Compile one candidate and verify every workload on the same elaborated RTL.

    adapter_runner must isolate candidate Python before calling it. Callers may
    reuse this for trusted post-synthesis netlists by supplying rtl_files and the
    same top_module, provided the adapter still comes from the identical source
    snapshot. All acceptance fields are computed here, never by the adapter.
    """
    root = Path(candidate_dir).resolve()
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    checks = []
    artifacts = {}
    result = {"status": "fail", "checks": checks, "metrics": {}, "artifacts": artifacts}
    if adapter_runner is None:
        result["status"] = "blocked"
        checks.append({"name": "adapter", "status": "blocked", "detail": "isolated adapter_runner required"})
        return result
    top = manifest.get("top_module", "")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", top):
        checks.append({"name": "top_module", "status": "fail", "detail": "invalid Verilog identifier"})
        return result
    sources = []
    for relative in manifest.get("rtl_files", []):
        source = (root / relative).resolve()
        if not source.is_relative_to(root) or not source.is_file():
            checks.append({"name": "rtl_files", "status": "fail", "detail": "RTL path escapes candidate or is missing"})
            return result
        sources.append(source)
    if not sources:
        checks.append({"name": "rtl_files", "status": "fail", "detail": "no RTL source files"})
        return result
    before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    original_sources = list(sources)
    if gate_netlist is not None:
        sources = [Path(gate_netlist).resolve(), *[Path(p).resolve() for p in (pdk_models or [])]]
        if any(not path.is_file() for path in sources):
            checks.append({"name": "gate_sources", "status": "fail", "detail": "trusted gate netlist/model missing"})
            return result
        artifacts["gate_source_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        checks.append({"name": "gate_simulation_scope", "status": "pass",
                       "detail": "functional gate models only; no SDF timing claim; separate STA required"})
    requests = _public_workloads(seed)
    responses = []
    public_requests = [{"schema_version": 1, "clock_hz": 50000000,
                       **{key: value for key, value in request.items()
                          if key not in ("nack_index", "address_nack")}}
                      for request in requests]
    batch_responses = None
    batch = getattr(adapter_runner, 'batch', None)
    if batch is not None:
        from .compiler_batch import CompilerBatchError
        try:
            batch_responses = batch(public_requests)
            if not isinstance(batch_responses, list) or len(batch_responses) != len(requests):
                raise ValueError('Incomplete compiler batch')
        except Exception as error:
            index = error.index if isinstance(error, CompilerBatchError) else 0
            blocked = isinstance(error, ToolUnavailable) or getattr(error, 'blocked', False)
            result['status'] = 'blocked' if blocked else 'fail'
            checks.append({'name': f'adapter_case_{index}', 'status': result['status'],
                           'detail': str(error)[:2000]})
            return result
    for index, request in enumerate(requests):
        public = public_requests[index]
        try:
            response = batch_responses[index] if batch_responses is not None else adapter_runner(public)
            response = json.loads(json.dumps(response, allow_nan=False))
            responses.append(validate_adapter(response, _read_counts(request)))
        except ToolUnavailable as error:
            result["status"] = "blocked"
            checks.append({"name": f"adapter_case_{index}", "status": "blocked", "detail": str(error)[:2000]})
            return result
        except Exception as error:
            checks.append({"name": f"adapter_case_{index}", "status": "fail", "detail": str(error)[:2000]})
            return result
    checks.append({"name": "adapter_contract", "status": "pass", "detail": f"{len(requests)} bounded JSON workloads"})
    # Peer inputs are deliberately produced after the compiler has returned all programs.
    # Never derive private inputs from a candidate-visible/public seed. A trusted
    # caller may supply the saved seed to reproduce a failure; ordinary hill
    # climbing always gets fresh entropy after every adapter has finished.
    private_peer_seed = secrets.randbits(256) if peer_seed is None else peer_seed
    if type(private_peer_seed) is not int or private_peer_seed < 0:
        raise ValueError("peer_seed must be a nonnegative integer")
    peer_rng = random.Random(private_peer_seed)
    nacks = [-1, 0, 1]
    peer_rng.shuffle(nacks)
    read_nacks = [False, True]
    peer_rng.shuffle(read_nacks)
    for request in requests:
        if request["kind"] == "i2c_write":
            request["nack_index"] = nacks.pop()
        elif request["kind"] == "i2c_read":
            request["address_nack"] = read_nacks.pop()
    (out / "private_replay.json").write_text(json.dumps({"public_seed": seed, "peer_seed": private_peer_seed})+"\n")
    artifacts["private_replay"] = str(out / "private_replay.json")
    scenario_lines, host_tasks = [], []
    for index, (request, stages) in enumerate(zip(requests, responses)):
        scenario, tasks = _scenario(index, request, stages, peer_rng)
        scenario_lines.append(scenario)
        host_tasks.extend(tasks)
    nonce = secrets.token_hex(24)
    total_stages = sum(map(len, responses))
    harness = (Path(__file__).parent / "protocols" / "harness.sv").read_text()
    for key, value in {"TOP": top, "HOST_TASKS": "\n".join(host_tasks),
                       "SCENARIOS": "\n".join(scenario_lines), "NONCE": nonce,
                       "CASES": str(len(requests)), "STAGES": str(total_stages)}.items():
        harness = harness.replace(f"@{key}@", value)
    tb_path = out / "trusted_tb.sv"
    tb_path.write_text(harness)
    (out / "public_workloads.json").write_text(json.dumps([
        {key: value for key, value in request.items() if key not in ("nack_index", "address_nack")}
        for request in requests], indent=2)+"\n")
    (out / "adapter_actions.json").write_text(json.dumps(responses)+"\n")
    artifacts.update({"testbench": str(tb_path), "compile_log": str(out / "compile.log"),
                      "simulation_log": str(out / "simulation.log"), "rtl_sha256": before})
    binary = out / "candidate.vvp"
    read_only = [root, *sorted({path.parent for path in sources if not path.is_relative_to(root)})]
    ok, detail = run_eda(["iverilog", "-g2012", "-s", "challenge_tb", "-o", str(binary),
                          str(tb_path), *map(str, sources)], cwd=out, readonly=read_only,
                         log_path=out / "compile.log", timeout=90)
    checks.append({"name": "compile", "status": "pass" if ok else "fail", "detail": detail})
    if not ok:
        return result
    ok, detail = run_eda(["vvp", str(binary)], cwd=out, readonly=read_only,
                         log_path=out / "simulation.log", timeout=1200 if gate_netlist is not None else 90)
    log = (out / "simulation.log").read_text(errors="replace")
    complete = re.findall(r"^VERIFIER_COMPLETE ([0-9a-f]+) cases=(\d+) stages=(\d+) checks=(\d+) cycles=(\d+)$", log, re.M)
    records = re.findall(r"^STAGE_COMPLETE case=(\d+) stage=(\d+) kind=([a-z0-9_]+) bits=(\d+) cycles=(\d+)$", log, re.M)
    expected_ids = {(str(i), str(j), requests[i]["kind"]) for i, stages in enumerate(responses) for j in range(len(stages))}
    observed_ids = {(row[0], row[1], row[2]) for row in records}
    completed = (ok and len(complete) == 1 and complete[0][0] == nonce
                 and int(complete[0][1]) == len(requests) and int(complete[0][2]) == total_stages
                 and len(records) == total_stages and observed_ids == expected_ids
                 and int(complete[0][3]) > total_stages*10
                 and all(int(row[4]) > 0 for row in records)
                 and not re.search(r"\b(?:FAIL|FATAL|ERROR)\b", log))
    checks.append({"name": "external_pin_protocols", "status": "pass" if completed else "fail",
                   "detail": f"{len(records)}/{total_stages} stages completed; {detail}"})
    after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in original_sources}
    same = after == before
    checks.append({"name": "same_hardware", "status": "pass" if same else "fail",
                   "detail": "one elaboration for all protocols and repeated live firmware replacement"})
    if completed and same:
        result["status"] = "pass"
        result["metrics"] = {"clock_hz": 50000000, "case_count": len(requests), "stage_count": total_stages,
                             "assertions": int(complete[0][3]), "simulation_cycles": int(complete[0][4]),
                             "workloads": [{"case": int(r[0]), "stage": int(r[1]), "kind": r[2],
                                            "useful_bits": int(r[3]), "elapsed_cycles": int(r[4])} for r in records]}
    (out / "functional.json").write_text(json.dumps(result, indent=2)+"\n")
    return result
