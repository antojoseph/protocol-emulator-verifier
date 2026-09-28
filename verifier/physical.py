"""Trusted, clean Tiny Tapeout CMOS5L physical acceptance pipeline.

No candidate configuration, scripts, reports, checkpoints, or cached results are
accepted. Missing tools/evidence fail closed. See docs/physical-verifier.md.
Flow integration is adapted from Tempo (Apache-2.0), attribution in
physical_support/NOTICE.tempo; official checks remain pinned upstream code.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = Path(__file__).resolve().parent / "physical_support"
LOCK = json.loads((SUPPORT / "flow-lock.json").read_text())
MAX_LOG_BYTES = 256 * 1024 * 1024
MAX_GENERATED_BYTES = 16 * 1024 * 1024 * 1024
WHEEL_LOCK = json.loads((SUPPORT / "python-wheels.json").read_text())
PYTHON_PACKAGES = {p["name"]: p["version"] for p in WHEEL_LOCK["packages"]}
CORNERS = ("nom_fast_1p32V_m40C", "nom_typ_1p20V_25C", "nom_slow_1p08V_125C")
PRECHECKS = {
    "KLayout pin label overlapping drawing", "KLayout SG13CMOS5L DRC",
    "KLayout zero area", "KLayout Checks", "Pin check", "Boundary check",
    "Layer check", "Cell name check", "Analog pin check",
}
ZERO_METRICS = (
    "design__lint_error__count", "design__lint_timing_construct__count",
    "design__inferred_latch__count", "design__instance_unmapped__count",
    "synthesis__check_error__count", "design__power_grid_violation__count",
    "design__disconnected_pin__count", "design__critical_disconnected_pin__count",
    "route__drc_errors", "route__antenna_violation__count",
    "magic__drc_error__count", "magic__illegal_overlap__count",
    "design__lvs_error__count", "design__lvs_device_difference__count",
    "design__lvs_net_difference__count", "design__lvs_property_fail__count",
    "design__lvs_unmatched_device__count", "design__lvs_unmatched_net__count",
    "design__lvs_unmatched_pin__count",
)
CORNER_ZERO_METRICS = (
    "timing__setup_vio__count", "timing__hold_vio__count",
    "timing__setup__tns", "timing__hold__tns",
    "design__max_slew_violation__count", "design__max_cap_violation__count",
    "timing__unannotated_net_filtered__count",
)
PORTS = {"clk": ("input", 1), "rst_n": ("input", 1), "ena": ("input", 1),
         "ui_in": ("input", 8), "uo_out": ("output", 8),
         "uio_in": ("input", 8), "uio_out": ("output", 8), "uio_oe": ("output", 8)}


class PhysicalError(RuntimeError):
    pass


class ToolsUnavailable(PhysicalError):
    pass


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")


def output_budget_error(directory, stage_log=None):
    """Bound generated disk use and each EDA log, including nested tool logs."""
    if stage_log is not None and Path(stage_log).stat().st_size > MAX_LOG_BYTES:
        return "exceeded the 256 MiB stage-log limit"
    total = 0
    for current, _, files in os.walk(directory, followlinks=False):
        for filename in files:
            path = Path(current) / filename
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except FileNotFoundError:
                continue
            total += size
            if path.suffix == ".log" and size > MAX_LOG_BYTES:
                return f"exceeded the 256 MiB log limit: {path}"
            if total > MAX_GENERATED_BYTES:
                return "exceeded the 16 GiB generated-output limit"
    return None


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise PhysicalError(f"missing or non-finite measured metric: {name}")
    return value


def check_metrics(metrics):
    """Fail closed on absent counts, nonfinite area/timing or missing PVT corners."""
    for name in ZERO_METRICS:
        if _finite(metrics.get(name), name) != 0:
            raise PhysicalError(f"{name} = {metrics[name]}, expected 0")
    area = _finite(metrics.get("design__instance__area__stdcell"), "standard-cell area")
    if not 0 < area <= 1289.28 * 710.64:
        raise PhysicalError(f"standard-cell area outside physical envelope: {area}")
    # This challenge currently allows synthesized digital logic only. Macro area
    # cannot be excluded from an area score by declaring a black box.
    if _finite(metrics.get("design__instance__area__macros"), "macro area") != 0:
        raise PhysicalError("hard macros are unsupported in contract v1")
    bbox = metrics.get("design__die__bbox", "")
    try:
        bbox = [float(v) for v in bbox.split()]
    except (ValueError, AttributeError):
        raise PhysicalError("missing die geometry metric")
    if len(bbox) != 4 or any(not math.isfinite(a) or abs(a-b) > 0.001 for a,b in zip(bbox, LOCK["die_um"])):
        raise PhysicalError(f"die geometry {bbox} differs from fixed 6x4 envelope")
    slacks = {}
    for corner in CORNERS:
        for base in CORNER_ZERO_METRICS:
            name = f"{base}__corner:{corner}"
            if _finite(metrics.get(name), name) != 0:
                raise PhysicalError(f"{name} = {metrics[name]}, expected 0")
        slacks[corner] = {}
        for kind in ("setup", "hold"):
            name = f"timing__{kind}__ws__corner:{corner}"
            value = _finite(metrics.get(name), name)
            if value < 0:
                raise PhysicalError(f"{corner} {kind} slack is negative: {value}")
            slacks[corner][kind + "_slack_ns"] = value
    return {"stdcell_area_um2": area, "clock_hz": 50_000_000,
            "die_area_um2": 1289.28 * 710.64, "timing": slacks,
            "fanout_advisories": metrics.get("design__max_fanout_violation__count")}


def check_def(path):
    text = Path(path).read_text()
    units = re.search(r"\bUNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    bbox = re.findall(r"\bDIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;", text)
    if not units or len(bbox) != 1 or int(units[1]) <= 0:
        raise PhysicalError("final DEF has missing/ambiguous rectangular geometry")
    values = [int(v) / int(units[1]) for v in bbox[0]]
    if any(abs(a-b) > 0.001 for a,b in zip(values, LOCK["die_um"])):
        raise PhysicalError(f"final DEF is not the required 6x4 footprint: {values}")
    return values


def check_precheck_xml(path):
    root = ET.parse(path).getroot()
    cases = root.findall(".//testcase")
    names = [case.get("name") for case in cases]
    if len(names) != len(PRECHECKS) or set(names) != PRECHECKS:
        raise PhysicalError(f"official precheck did not complete exactly the required checks: {names}")
    if any(case.find(kind) is not None for case in cases for kind in ("failure", "error", "skipped")):
        raise PhysicalError("official precheck contains a failure, error, or skip")
    return names


def check_sta_reports(sta_dir):
    """Use actual post-route reports; a zero status or empty check is insufficient."""
    for corner in CORNERS:
        directory = Path(sta_dir) / corner
        checks = (directory / "checks.rpt").read_text()
        marker = "check_setup -verbose -unconstrained_endpoints -multiple_clock -no_clock -no_input_delay -loops -generated_clocks"
        if checks.count(marker) != 1:
            raise PhysicalError(f"missing complete timing constraint audit for {corner}")
        audit = checks.split(marker, 1)[1]
        if re.search(r"\b(?:warning|error)\b", audit, re.I):
            raise PhysicalError(f"timing constraint audit has warnings/errors for {corner}: {audit[-2000:]}")
        # Unconstrained start/end points in report_checks are explicitly labeled.
        if re.search(r"(?:Path Group:\s*\*\*unconstrained\*\*|\(unconstrained\))", checks, re.I):
            raise PhysicalError(f"unconstrained timing paths in {corner}")
        for name in ("min.rpt", "max.rpt"):
            report = (directory / name).read_text()
            if "Startpoint:" not in report or "Endpoint:" not in report or "slack" not in report.lower():
                raise PhysicalError(f"empty timing path coverage: {corner}/{name}")
        clock = (directory / "clock.rpt").read_text()
        clocks = re.findall(r"^Clock:\s*(\S+)\s*$", clock, re.M)
        periods = re.findall(r"^Period:\s*([0-9.eE+-]+)\s*$", clock, re.M)
        if clocks != ["clk"] or len(periods) != 1 or abs(float(periods[0])-20) > 1e-8:
            raise PhysicalError(f"missing/incorrect physical clock in {corner}")
        if (directory / "unpropagated.rpt").read_text().strip():
            raise PhysicalError(f"post-route clock is not propagated in {corner}")


def check_lvs_log(text):
    devices = re.findall(r"Devices to compare: layout=(\d+)\s+schematic=(\d+)", text)
    if ("Congratulations! Netlists match." not in text or
            "Netlists don't match" in text or not devices or
            any(int(a) <= 0 or int(b) <= 0 for a,b in devices)):
        raise PhysicalError("final GDS LVS lacks an explicit, nonempty netlist match")
    return [[int(a), int(b)] for a,b in devices]


def verify_precheck_wheels(tools_root):
    """Verify every executable package byte against checksum-pinned wheel files."""
    root = Path(tools_root)
    packages = root / "precheck-python"
    expected = set()
    if not packages.is_dir():
        raise ToolsUnavailable("precheck wheels missing; run scripts/setup_physical.py")
    for spec in WHEEL_LOCK["packages"]:
        wheel = root / "wheels" / spec["filename"]
        if not wheel.is_file() or sha256(wheel) != spec["sha256"]:
            raise ToolsUnavailable(f"missing or changed pinned wheel: {wheel}")
        with zipfile.ZipFile(wheel) as archive:
            for item in archive.infolist():
                if item.is_dir():
                    continue
                relative = Path(item.filename)
                if relative.is_absolute() or ".." in relative.parts:
                    raise ToolsUnavailable("invalid path in pinned wheel")
                expected.add(item.filename)
                path = packages / relative
                if path.is_symlink() or not path.is_file() or sha256(path) != hashlib.sha256(archive.read(item)).hexdigest():
                    raise ToolsUnavailable(f"missing or changed extracted wheel file: {path}")
    actual = {str(p.relative_to(packages)) for p in packages.rglob("*") if not p.is_dir()}
    if actual != expected:
        raise ToolsUnavailable("unexpected precheck package files; use an intact setup cache")
    return packages


def _repository_verified(path, spec, allowed_untracked=()):
    path = Path(path)
    if not path.is_dir():
        raise ToolsUnavailable(f"missing {path}; run scripts/setup_physical.py")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(path), *args], text=True, timeout=60).strip()
    if git("rev-parse", "HEAD") != spec["commit"]:
        raise ToolsUnavailable(f"incorrect pinned revision in {path}")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise ToolsUnavailable(f"modified tracked dependency: {path}")
    untracked = set(filter(None, git("ls-files", "--others", "--exclude-standard").splitlines()))
    if untracked - set(allowed_untracked):
        raise ToolsUnavailable(f"untracked dependency files in {path}: {sorted(untracked)[:10]}")


def _only(directory, pattern):
    paths = list(Path(directory).glob(pattern))
    if len(paths) != 1 or not paths[0].is_file() or paths[0].stat().st_size == 0:
        raise PhysicalError(f"expected one fresh nonempty {pattern} under {directory}")
    return paths[0].resolve()


def _copy_tracked(source, destination):
    source, destination = Path(source), Path(destination)
    files = subprocess.check_output(["git", "-C", str(source), "ls-files", "-z"], timeout=60).decode().split("\0")
    for name in filter(None, files):
        path = source / name
        if path.is_symlink():
            target = path.resolve()
            if source.resolve() not in target.parents:
                raise ToolsUnavailable(f"external dependency symlink: {path}")
        output = destination / name
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)


def _check_ports(path, top):
    modules = json.loads(Path(path).read_text())["modules"]
    ports = modules[top]["ports"]
    actual = {name: (value["direction"], len(value["bits"])) for name,value in ports.items()}
    if actual != PORTS:
        raise PhysicalError(f"top-level port interface differs from Tiny Tapeout contract: {actual}")


def prepare_functional_models(model_paths, output_dir):
    """Convert pinned PDK timing models to zero-delay Icarus functional models.

    Trusted adaptation only: preserves cell logic/UDP truth tables, removes
    unsupported specify checks, and directly aliases delayed timing inputs.
    Metadata records both hashes; these files are never used for timing/STA.
    """
    from .physical_support.functional_models import adapt
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = []
    for source in map(Path, model_paths):
        output = output_dir / source.name
        if output.exists():
            raise PhysicalError(f"functional model output already exists: {output}")
        adapt(source, output)
        result.append(str(output.resolve()))
    return result


def run_physical(candidate_path, workdir, config=None, timeout=14400):
    """Return structured pass/fail/blocked, measured metrics, and fresh artifacts.

    Organizer-only config keys: tools_root, jobs, top_module, rtl_files. No
    physical constraints or candidate flow settings may be overridden.
    """
    started = time.monotonic()
    config = dict(config or {})
    result = {"status": "fail", "checks": [], "metrics": {}, "artifacts": {},
              "provenance": {"flow": LOCK}, "gate_netlist": None, "pdk_models": []}
    def check(name, detail):
        result["checks"].append({"name": name, "status": "pass", "detail": detail})
    try:
        invalid_keys = set(config) - {"tools_root", "jobs", "top_module", "rtl_files"}
        if invalid_keys:
            raise PhysicalError(f"unrecognized physical configuration keys: {sorted(invalid_keys)}")
        candidate = Path(candidate_path).resolve()
        work = Path(workdir).resolve()
        if any(c in str(work) for c in "{}\n\r\0"):
            raise PhysicalError("physical working path contains unsupported Tcl characters")
        if work == candidate or candidate in work.parents or work in candidate.parents:
            raise PhysicalError("physical output must be outside candidate tree")
        if work.exists() and any(work.iterdir()):
            raise PhysicalError("physical output directory must be fresh; checkpoints are forbidden")
        work.mkdir(parents=True, exist_ok=True)
        manifest = json.loads((candidate / "candidate.json").read_text())
        top = config.get("top_module", manifest.get("top_module"))
        sources = config.get("rtl_files", manifest.get("rtl_files"))
        if not isinstance(top, str) or not re.fullmatch(r"tt_um_[A-Za-z0-9_]+", top):
            raise PhysicalError("invalid Tiny Tapeout top_module")
        if not isinstance(sources, list) or not sources or len(set(sources)) != len(sources):
            raise PhysicalError("rtl_files must be a nonempty unique list")
        source_hashes = {}
        names = []
        for index, relative in enumerate(sources):
            if not isinstance(relative, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+\.(?:v|sv)", relative):
                raise PhysicalError("unsupported RTL source filename")
            source = candidate / relative
            if Path(relative).is_absolute() or ".." in Path(relative).parts or source.is_symlink() or candidate not in source.resolve().parents:
                raise PhysicalError(f"RTL escapes candidate: {relative}")
            data = source.read_bytes()
            source_hashes[relative] = hashlib.sha256(data).hexdigest()
            # Flat controlled names prevent config/code injection through filenames.
            name = f"rtl_{index:04d}{source.suffix}"
            (work / "src").mkdir(exist_ok=True)
            (work / "src" / name).write_bytes(data)
            names.append(name)
        result["provenance"]["source_sha256"] = source_hashes
        tools_root = Path(config.get("tools_root", os.getenv("JANES_PHYSICAL_TOOLS", ROOT / ".tools/physical"))).resolve()
        pdk = tools_root / "pdk"
        tt = tools_root / "tt"
        _repository_verified(tt, LOCK["support_tools"])
        _repository_verified(pdk, LOCK["pdk"], (LOCK["pdk"]["name"] + "/SOURCES",))
        packages = verify_precheck_wheels(tools_root)
        result["provenance"]["precheck_python_wheels"] = WHEEL_LOCK
        try:
            subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
            subprocess.run(["docker", "image", "inspect", LOCK["container"]], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolsUnavailable(f"pinned Docker engine/image unavailable: {error}")
        check("pinned_tools", "verified exact support/PDK commits and pinned Docker image")
        _copy_tracked(tt, work / "tt")
        (work / "tt/precheck/reports").mkdir(exist_ok=True)
        (work / "logs").mkdir(exist_ok=True)
        (work / "trusted").mkdir(exist_ok=True)
        jobs = config.get("jobs", 4)
        if isinstance(jobs, bool) or not isinstance(jobs, int) or not 1 <= jobs <= 64:
            raise PhysicalError("jobs must be an integer in 1..64")
        commands = []
        container_names = []
        def run(command, label, cwd=None, env=None):
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise PhysicalError("physical pipeline timed out")
            log = work / "logs" / (label + ".log")
            commands.append({"stage": label, "command": [str(c) for c in command], "log": str(log)})
            with log.open("w") as stream:
                process = subprocess.Popen([str(c) for c in command], cwd=cwd or work,
                    env=env, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
                deadline = time.monotonic() + remaining
                last_disk_check = 0.0
                violation = None
                while process.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        violation = f"{label} timed out"
                    elif os.fstat(stream.fileno()).st_size > MAX_LOG_BYTES:
                        violation = f"{label} exceeded the 256 MiB stage-log limit"
                    elif now-last_disk_check > 5:
                        violation = output_budget_error(work, log)
                        last_disk_check = now
                    if violation:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=20)
                        # Includes the nested Docker calls made by the official
                        # precheck wrapper, not only top-level hardening.
                        for name in container_names:
                            subprocess.run(["docker", "rm", "-f", name],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                        raise PhysicalError(f"{violation}; see {log}")
                    time.sleep(0.25)
                completed = process
                violation = output_budget_error(work, log)
                if violation:
                    raise PhysicalError(f"{label} {violation}; see {log}")
            commands[-1]["returncode"] = completed.returncode
            if completed.returncode:
                raise PhysicalError(f"{label} exited {completed.returncode}; see {log}")
            return log
        def container(*args, precheck=False):
            name = "janes-physical-" + uuid.uuid4().hex[:16]
            container_names.append(name)
            command = ["docker", "run", "--rm", "--name", name,
                    "--platform=linux/amd64", "--network=none", "--cap-drop=ALL",
                    "--security-opt=no-new-privileges", "--pids-limit=2048", "--memory=16g", "--cpus=4", "--read-only",
                    "--tmpfs", "/tmp:rw,exec,size=4g", "--user", f"{os.getuid()}:{os.getgid()}",
                    "--volume", f"{work}:{work}", "--volume", f"{work / 'src'}:{work / 'src'}:ro",
                    "--volume", f"{work / 'trusted'}:{work / 'trusted'}:ro",
                    "--volume", f"{work / 'tt'}:{work / 'tt'}:ro",
                    "--volume", f"{work / 'tt/precheck/reports'}:{work / 'tt/precheck/reports'}:rw",
                    "--volume", f"{pdk}:{pdk}:ro", "--workdir", str(work),
                    "--env", f"PDK_ROOT={pdk}", "--env", f"PDK={LOCK['pdk']['name']}",
                    "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", f"HOME={work}"]
            # The dependency probe runs before the verifier creates metadata.
            if (work / "info.yaml").exists():
                command += ["--volume", f"{work / 'info.yaml'}:{work / 'info.yaml'}:ro"]
            if precheck:
                command += ["--volume", f"{packages}:{packages}:ro", "--env", f"PYTHONPATH={packages}", "--env", f"LD_LIBRARY_PATH={WHEEL_LOCK['container_library_path']}"]
            return command + [LOCK["container"], *map(str,args)]
        result["provenance"]["commands"] = commands
        dependency_probe = "import sys,json,importlib.metadata,gdstk,klayout.db,klayout.rdb,yaml; assert sys.version_info[:2]==(3,13); versions={name: importlib.metadata.version(name) for name in " + repr(list(PYTHON_PACKAGES)) + "}; assert versions==" + repr(PYTHON_PACKAGES) + "; print(json.dumps(versions))"
        run(container("python3", "-c", dependency_probe, precheck=True), "precheck_dependencies")
        check("precheck_dependencies", "checksum-verified Python 3.13 Linux wheels loaded inside pinned image")
        info = {"project": {"title": "Verified programmable protocol engine", "author": "Candidate contributors",
                "description": "Yukon protocol emulator candidate", "language": "Verilog", "clock_hz": 50_000_000,
                "tiles": "6x4", "top_module": top, "source_files": names},
                "pinout": {f"{bus}[{i}]": f"{bus.upper()}_{i}" for bus in ("ui", "uo", "uio") for i in range(8)}, "yaml_version": 6}
        # JSON is a YAML subset; the official precheck reads this immutable file.
        _write_json(work / "info.yaml", info)
        flow_config = json.loads((SUPPORT / "config.json").read_text())
        flow_config.pop("//", None)
        flow_config.update({"DESIGN_NAME": top, "VERILOG_FILES": [f"dir::{name}" for name in names],
            "DIE_AREA": "0 0 1289.28 710.64", "FP_DEF_TEMPLATE": "dir::../tt/tech/ihp-sg13cmos5l/def/tt_block_6x4_pgvdd.def",
            "VDD_PIN": "VPWR", "GND_PIN": "VGND", "RT_MAX_LAYER": "Metal4", "CLOCK_PERIOD": 20,
            "CLOCK_PORT": "clk", "STA_CORNERS": list(CORNERS), "TIMING_VIOLATION_CORNERS": ["*"],
            "RUN_MAGIC_DRC": True, "RUN_LVS": True, "RUN_DRT": True, "RUN_FILL_INSERTION": True,
            "IO_DELAY_CONSTRAINT": 20, "CLOCK_UNCERTAINTY_CONSTRAINT": 0.25,
            "OUTPUT_CAP_LOAD": 6, "OPENROAD_THREADS": jobs})
        _write_json(work / "src/config_merged.json", flow_config)
        result["provenance"]["config_sha256"] = sha256(work / "src/config_merged.json")
        yosys = "read_verilog -sv " + " ".join(str(work / "src" / n) for n in names) + f"\nhierarchy -check -top {top}\nproc\nwrite_json {work / 'ports.json'}\n"
        (work / "trusted/ports.ys").write_text(yosys)
        run(container("yosys", "-Q", "-T", "-s", work / "trusted/ports.ys"), "ports")
        _check_ports(work / "ports.json", top)
        check("tiny_tapeout_ports", "exact direction and width for all eight Tiny Tapeout ports")
        run_dir = work / "runs/authoritative"
        run_dir.mkdir(parents=True)
        run(container("librelane", "--pdk-root", pdk, "--pdk", LOCK["pdk"]["name"],
                "--manual-pdk", "--run-tag", "authoritative", "--force-run-dir", run_dir,
                "--jobs", jobs, "--override-config", f"OPENROAD_THREADS={jobs}",
                "--condensed", work / "src/config_merged.json"), "harden")
        states = sorted(run_dir.glob("[0-9]*-*/state_out.json"), key=lambda p: int(p.parent.name.split("-",1)[0]))
        if not states or not states[-1].parent.name.endswith("-misc-reportmanufacturability"):
            raise PhysicalError("clean physical flow did not reach final manufacturability stage")
        if not any(p.parent.name.endswith("-yosys-synthesis") for p in states):
            raise PhysicalError("fresh synthesis evidence absent")
        sta = [p.parent for p in states if p.parent.name.endswith("-openroad-stapostpnr")]
        if len(sta) != 1:
            raise PhysicalError("missing/ambiguous post-route multicorner STA stage")
        metrics = json.loads(states[-1].read_text())["metrics"]
        result["metrics"] = check_metrics(metrics)
        check_sta_reports(sta[0])
        check("physical_metrics_and_timing", "zero DRC/LVS/antenna/electrical violations; all three supplied PVT corners at nominal RC; constrained propagated 50MHz clock")
        artifacts = {key: _only(run_dir, pattern) for key,pattern in {
            "gds": "final/gds/*.gds", "netlist": "final/nl/*.nl.v", "odb": "final/odb/*.odb",
            "def": "final/def/*.def", "lef": "final/lef/*.lef"}.items()}
        check_def(artifacts["def"])
        result["artifacts"] = {key: {"path": str(path), "sha256": sha256(path)} for key,path in artifacts.items()}
        precheck_command = container("python3", work / "tt/precheck/precheck.py",
            "--gds", artifacts["gds"], "--tech", LOCK["pdk"]["name"], precheck=True)
        precheck_command[precheck_command.index("--workdir") + 1] = str(work / "tt/precheck")
        run(precheck_command, "official_precheck")
        check("official_precheck", check_precheck_xml(work / "tt/precheck/reports/results.xml"))
        geometry_script = work / "trusted/gds_geometry.py"
        geometry_script.write_text("import gdstk,json,sys\nlib=gdstk.read_gds(sys.argv[1])\ntops=lib.top_level()\nassert len(tops)==1\nb=tops[0].bounding_box()\njson.dump({'bbox':[v for xy in b for v in xy]},open(sys.argv[2],'w'))\n")
        run(container("python3", geometry_script, artifacts["gds"], work / "gds_geometry.json", precheck=True), "gds_geometry")
        bbox = json.loads((work / "gds_geometry.json").read_text())["bbox"]
        if len(bbox) != 4 or any(not math.isfinite(a) or abs(a-b) > .001 for a,b in zip(bbox, LOCK["die_um"])):
            raise PhysicalError(f"actual GDS extent differs from fixed 6x4 geometry: {bbox}")
        check("actual_gds_and_def_geometry", bbox)
        # Supplement flow Netgen LVS with extraction from final delivered GDS.
        pdk_dir = pdk / LOCK["pdk"]["name"]
        master = pdk_dir / "libs.ref/sg13cmos5l_stdcell/cdl/sg13cmos5l_stdcell.cdl"
        lvs_runner = pdk_dir / "libs.tech/klayout/tech/lvs/run_lvs.py"
        lvs_dir = work / "gds_lvs"
        lvs_dir.mkdir()
        cdl = lvs_dir / "routed.cdl"
        tcl = work / "trusted/export_cdl.tcl"
        tcl.write_text(f"read_db {{{artifacts['odb']}}}\nwrite_cdl -masters {{{master}}} -include_fillers {{{cdl}}}\n")
        run(container("openroad", "-exit", "-no_splash", tcl), "export_cdl")
        cdl.write_text(f'.include "{master}"\n' + cdl.read_text())
        lvs_log = run(container("python3", lvs_runner, "--layout", artifacts["gds"], "--netlist", cdl,
                 "--run_dir", lvs_dir / "check", "--run_mode", "deep", "--disable_tap_extraction"), "gds_lvs")
        logs = lvs_log.read_text(errors="replace") + "\n" + "\n".join(p.read_text(errors="replace") for p in (lvs_dir / "check").glob("*.log"))
        check("final_gds_transistor_lvs", check_lvs_log(logs))
        for name,artifact in result["artifacts"].items():
            if sha256(artifact["path"]) != artifact["sha256"]:
                raise PhysicalError(f"generated {name} changed during verification")
        for relative,digest in source_hashes.items():
            if sha256(candidate / relative) != digest:
                raise PhysicalError(f"candidate source changed during verification: {relative}")
        models = sorted((pdk_dir / "libs.ref/sg13cmos5l_stdcell/verilog").glob("*.v"))
        if not models:
            raise PhysicalError("pinned PDK functional models absent")
        result["gate_netlist"] = str(artifacts["netlist"])
        result["pdk_models"] = prepare_functional_models(models, work / "functional_models")
        result["provenance"]["functional_model_sha256"] = {str(p): sha256(p) for p in result["pdk_models"]}
        result["status"] = "pass"
        check("fresh_artifact_provenance", "source, fixed config, GDS and netlist hashes retained; no resume/checkpoint used")
    except ToolsUnavailable as error:
        result["status"] = "blocked"
        result["checks"].append({"name": "physical_tooling", "status": "blocked", "detail": str(error)})
    except (PhysicalError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, ET.ParseError) as error:
        result["checks"].append({"name": "physical_acceptance", "status": "fail", "detail": str(error)})
    result["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # No score is produced here. The parent must require a fresh functional gate
    # run against gate_netlist before any physical result can be promoted.
    output = Path(workdir)
    if output.is_dir():
        _write_json(output / "physical-result.json", result)
    return result
