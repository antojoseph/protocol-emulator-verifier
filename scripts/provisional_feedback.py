#!/usr/bin/env python3
"""Read completed early physical metrics; never execute or accept a candidate.

Inputs must be organizer-owned evaluation evidence. This is an observer, not an
authenticator for participant-supplied reports, a cache, or a signoff path.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from verifier.candidate import validate
from verifier.cli import harness_hashes
from verifier.common import VerificationError

# Exact first 44 stages of the accepted pinned Classic flow. A different flow
# needs a reviewed observer update; never guess a similarly named milestone.
EARLY_STAGES = (
    'verilator-lint', 'checker-linttimingconstructs', 'checker-linterrors',
    'checker-lintwarnings', 'yosys-jsonheader', 'yosys-synthesis',
    'checker-yosysunmappedcells', 'checker-yosyssynthchecks',
    'checker-netlistassignstatements', 'openroad-checksdcfiles',
    'openroad-checkmacroinstances', 'openroad-staprepnr', 'openroad-floorplan',
    'openroad-dumprcvalues', 'odb-checkmacroantennaproperties',
    'odb-setpowerconnections', 'odb-manualmacroplacement', 'openroad-cutrows',
    'openroad-tapendcapinsertion', 'odb-addpdnobstructions',
    'openroad-generatepdn', 'odb-removepdnobstructions',
    'odb-addroutingobstructions', 'openroad-globalplacementskipio',
    'openroad-ioplacement', 'odb-customioplacement', 'odb-applydeftemplate',
    'openroad-globalplacement', 'odb-writeverilogheader',
    'checker-powergridviolations', 'openroad-stamidpnr',
    'openroad-repairdesignpostgpl', 'odb-manualglobalplacement',
    'openroad-detailedplacement', 'openroad-cts', 'openroad-stamidpnr-1',
    'openroad-resizertimingpostcts', 'openroad-stamidpnr-2',
    'openroad-globalrouting', 'openroad-checkantennas',
    'openroad-repairdesignpostgrt', 'odb-diodesonports',
    'openroad-repairantennas', 'openroad-stamidpnr-3',
)
DIRECT_METRICS = {
    'design__instance__area__stdcell', 'design__instance__count__stdcell',
    'design__instance__utilization__stdcell', 'global_route__wirelength',
    'global_route__vias', 'route__wirelength__estimated',
}
CORNER_METRIC = re.compile(
    r'(?:timing__(?:setup|hold)__(?:ws|wns|tns)|'
    r'design__max_(?:slew|cap|fanout)_violation__count)__corner:'
    r'(?:nom_fast_1p32V_m40C|nom_typ_1p20V_25C|nom_slow_1p08V_125C)\Z')
RUNTIME = re.compile(rb'(\d+):([0-5]\d):([0-5]\d)\.(\d+)\Z')


class FeedbackError(ValueError):
    pass


def _json(data, duplicate_keys=None):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result or (duplicate_keys is not None and key in duplicate_keys):
                if duplicate_keys is None:
                    raise FeedbackError('Duplicate JSON key: ' + key)
                # OpenROAD may append successive metric values in one object.
                # Do not guess which one is authoritative, even if equal.
                duplicate_keys.add(key)
                result.pop(key, None)
                continue
            result[key] = value
        return result
    def invalid(value):
        raise FeedbackError('Non-finite JSON token: ' + value)
    try:
        value = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as error:
        raise FeedbackError('Invalid JSON: ' + str(error)) from error
    if not isinstance(value, dict):
        raise FeedbackError('Expected JSON object')
    return value


class Evidence:
    def __init__(self, root):
        self.root = Path(root).absolute()
        self.hashes = {}

    def read(self, path, limit=4 * 1024 * 1024):
        path = Path(path).absolute()
        if not path.is_relative_to(self.root):
            raise FeedbackError('Evidence path escapes evaluation')
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise FeedbackError('Symlink evidence is forbidden')
            if parent == self.root:
                break
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise FeedbackError('Evidence must be a regular file')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise FeedbackError('Evidence exceeds size limit')
        digest = hashlib.sha256(data).hexdigest()
        old = self.hashes.setdefault(path, (digest, limit))
        if old != (digest, limit):
            raise FeedbackError('Evidence changed during observation')
        return data

    def recheck(self):
        for path, (_, limit) in list(self.hashes.items()):
            self.read(path, limit)


def observe(evaluation, candidate):
    """One bounded, read-only snapshot; missing/partial stages remain pending."""
    result = {
        'schema_version': 1, 'status': 'feedback_unavailable',
        'mode': 'provisional_observation', 'accepted': False, 'score': None,
        'evaluation': str(Path(evaluation).absolute()),
        'scope': 'Completed-stage observations only; no new tests or signoff performed.',
        'required_for_acceptance': 'A fresh complete full verifier run with every mandatory gate.',
    }
    try:
        evidence = Evidence(evaluation)
        record = _json(evidence.read(evidence.root / 'result.json', 8 * 1024 * 1024))
        if record.get('status') not in ('running', 'pass'):
            raise FeedbackError('Evaluation has failed, is blocked, or has unknown status')
        if record.get('mode') != 'full':
            raise FeedbackError('Early physical observations require a full evaluation')
        expected = record.get('candidate', {}).get('source_sha256')
        if not expected:
            raise FeedbackError('Evaluation has no source identity yet')
        trusted_harness = harness_hashes()
        if record.get('harness_sha256') != trusted_harness:
            raise FeedbackError('Evaluation harness differs from this observer checkout')
        _, current = validate(candidate)
        _, snapshot = validate(evidence.root / 'candidate')
        if expected != current or expected != snapshot:
            raise FeedbackError('Evaluation source differs from current candidate or snapshot')
        stages = record.get('stages', {})
        if any(stages.get(k, {}).get('status') != 'pass' for k in ('synthesis', 'functional')):
            raise FeedbackError('Fresh synthesis and RTL functional stages are not complete')

        run_dir = evidence.root / 'physical/runs/authoritative'
        complete, metrics, runtime_seconds = [], {}, 0.0
        ambiguous_metrics = {}
        pending = None
        for number, suffix in enumerate(EARLY_STAGES, 1):
            name = f'{number:02d}-{suffix}'
            matches = list(run_dir.glob(f'{number:02d}-*'))
            if not matches:
                pending = name
                break
            if len(matches) != 1 or matches[0].name != name or not matches[0].is_dir():
                raise FeedbackError('Unexpected or ambiguous pinned flow stage: ' + name)
            stage = matches[0]
            try:
                marker = evidence.read(stage / 'runtime.txt', 128)
            except FileNotFoundError:
                pending = name
                break
            duration = RUNTIME.fullmatch(marker)
            if duration is None:
                pending = name  # Do not read a still-writing stage's outputs.
                break
            state = _json(evidence.read(stage / 'state_out.json'))
            if not isinstance(state.get('metrics'), dict):
                raise FeedbackError('Completed stage lacks validated output state: ' + name)
            runtime_seconds += int(duration[1]) * 3600 + int(duration[2]) * 60 + float(duration[3] + b'.' + duration[4])
            complete.append(name)
            direct_path = stage / 'or_metrics_out.json'
            try:
                duplicates = set()
                direct = _json(evidence.read(direct_path), duplicates)
            except FileNotFoundError:
                continue
            if duplicates:
                ambiguous_metrics[name] = sorted(duplicates)
            for key, value in direct.items():
                if key not in DIRECT_METRICS and not CORNER_METRIC.fullmatch(key):
                    continue
                if type(value) not in (int, float) or not math.isfinite(value):
                    raise FeedbackError('Invalid direct numeric metric: ' + key)
                metrics[key] = {
                    'value': value, 'stage': name,
                    'source': str(direct_path.relative_to(evidence.root)),
                    'sha256': evidence.hashes[direct_path][0],
                    'interpretation': 'Measured at the named stage; not final signoff.',
                }
        # Data from different physical states is not one coherent final result.
        # Keep only corners directly computed by the latest observed STA stage.
        timing_stages = [name for name in complete if name.startswith(('12-', '31-', '36-', '38-', '44-'))]
        if timing_stages:
            latest_timing = timing_stages[-1]
            metrics = {key: value for key, value in metrics.items()
                       if not CORNER_METRIC.fullmatch(key) or value['stage'] == latest_timing}
        evidence.recheck()
        if validate(candidate)[1] != current or validate(evidence.root / 'candidate')[1] != snapshot:
            raise FeedbackError('Candidate changed during observation')
        if harness_hashes() != trusted_harness:
            raise FeedbackError('Harness changed during observation')
        result.update(
            status='feedback_available' if complete else 'pending',
            source_run_status=record['status'],
            source_sha256=current, harness_sha256=trusted_harness,
            completed_early_stages=complete,
            next_incomplete_early_stage=pending,
            early_target_complete=len(complete) == len(EARLY_STAGES),
            observed_tool_runtime_seconds=round(runtime_seconds, 6),
            metrics=metrics,
            omitted_duplicate_metric_keys=ambiguous_metrics,
            fast_observation={
                'synthesis': stages['synthesis'].get('metrics', {}),
                'functional': {k: stages['functional'].get('metrics', {}).get(k)
                               for k in ('case_count', 'stage_count', 'assertions')},
                'reexecuted_by_observer': False,
                'scope': 'Previously completed within the named evaluation; not a new verdict.',
            },
            evidence_sha256={str(p.relative_to(evidence.root)): h for p, (h, _) in evidence.hashes.items()},
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError,
            RuntimeError, VerificationError) as error:
        result['reason'] = str(error)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evaluation', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    args = parser.parse_args()
    result = observe(args.evaluation, args.candidate)
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0 if result['status'] in ('feedback_available', 'pending') else 1


if __name__ == '__main__':
    raise SystemExit(main())
