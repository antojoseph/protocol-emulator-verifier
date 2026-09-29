from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import traceback

from .candidate import validate, snapshot
from .common import ROOT, VerificationError, ToolUnavailable, digest, finite_number, read_json, write_json
from .isolation import compiler
from .synthesis import run_synthesis


def harness_hashes():
    return {p.relative_to(ROOT).as_posix(): digest(p)
            for p in sorted((ROOT / 'verifier').rglob('*'))
            if p.is_file() and '__pycache__' not in p.parts}


def evaluate(candidate, output, *, mode='fast', seed=1, physical_timeout=14400, physical_schedule='parallel', compiler_workers=1):
    output = Path(output).resolve()
    candidate = Path(candidate).resolve()
    if output == candidate or output.is_relative_to(candidate):
        raise VerificationError('Output directory must be outside the candidate')
    output.mkdir(parents=True, exist_ok=False)
    result = {'schema_version': 1, 'status': 'running', 'mode': mode,
              'accepted': False, 'score': None, 'provisional_score': None,
              'seed': seed, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'compiler_workers': compiler_workers,
              'contract': 'js-protocol-emulator-v1', 'checks': [], 'stages': {},
              'harness_sha256': harness_hashes(),
              'scope': 'Technical benchmark validation; publication and official submission are separate.'}
    result_path = output / 'result.json'
    write_json(result_path, result)

    def stage(name, function):
        print('[' + name + '] running', flush=True)
        data = function()
        if not isinstance(data, dict) or data.get('status') not in ('pass', 'fail', 'blocked'):
            raise VerificationError('Malformed trusted stage result: ' + name)
        result['stages'][name] = data
        write_json(result_path, result)
        if data['status'] == 'blocked':
            raise ToolUnavailable(name + ' blocked: ' + str(data.get('detail', data.get('checks', ''))))
        if data['status'] != 'pass':
            raise VerificationError(name + ' failed: ' + str(data.get('detail', data.get('checks', ''))))
        print('[' + name + '] pass', flush=True)
        return data

    try:
        manifest, hashes = validate(candidate)
        source = snapshot(candidate, output / 'candidate', hashes)
        # Validate the exact copied bytes too: source edits between validation
        # and hashing must never introduce unchecked simulator constructs.
        manifest, snapshot_hashes = validate(source)
        if snapshot_hashes != hashes:
            raise VerificationError('Candidate changed while validating immutable snapshot')
        result['candidate'] = {'name': manifest['name'], 'source_sha256': hashes}
        result['checks'].append({'name': 'source_license_manifest', 'status': 'pass'})
        docker = shutil.which('docker')
        if not docker:
            raise ToolUnavailable('Docker is required; install/start it and run ./setup.sh')
        image = read_json(ROOT / 'verifier/physical_support/flow-lock.json')['container']
        try:
            for command in ([docker, 'info', '--format', '{{.ServerVersion}}'],
                            [docker, 'image', 'inspect', '--format', '{{.Id}}', image]):
                subprocess.run(command, check=True, capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError) as error:
            raise ToolUnavailable('Pinned Docker runtime/image unavailable; run ./setup.sh: ' + str(error))
        synth = stage('synthesis', lambda: run_synthesis(source, manifest, output / 'synthesis'))
        from .functional import run_functional
        functional = stage('functional', lambda: run_functional(
            source, manifest, output / 'functional', seed=seed,
            adapter_runner=compiler(source, manifest, output / 'compiler', workers=compiler_workers)))
        result['provisional_score'] = finite_number(synth['metrics']['provisional_score'], 'provisional_score', positive=True)
        if mode == 'full':
            from .physical import run_physical
            physical = stage('physical', lambda: run_physical(
                source, output / 'physical', timeout=physical_timeout, config={'schedule': physical_schedule}))
            # Gate-level checking is mandatory and uses this candidate's fresh mapped netlist.
            gate_netlist = physical.get('gate_netlist') or physical.get('artifacts', {}).get('gate_netlist')
            pdk_models = physical.get('pdk_models') or physical.get('artifacts', {}).get('pdk_models')
            if not gate_netlist or not pdk_models:
                raise VerificationError('Physical stage did not supply netlist and PDK models for gate verification')
            gates = stage('gates', lambda: run_functional(
                source, manifest, output / 'gates', seed=seed,
                adapter_runner=compiler(source, manifest, output / 'gate-compiler', workers=compiler_workers),
                gate_netlist=gate_netlist, pdk_models=pdk_models))
            area = finite_number(physical['metrics']['stdcell_area_um2'], 'stdcell_area_um2', positive=True)
            result['score'] = 1_000_000 / area
            result['accepted'] = True
        for name, expected in hashes.items():
            if digest(source / name) != expected:
                raise VerificationError('Snapshot changed during evaluation: ' + name)
        if harness_hashes() != result['harness_sha256']:
            raise VerificationError('Harness changed during evaluation')
        result['status'] = 'pass'
        result['checks'].append({'name': 'source_and_harness_unchanged', 'status': 'pass'})
    except ToolUnavailable as error:
        result.update(status='blocked', accepted=False, score=None, error=str(error))
    except Exception as error:
        result.update(status='fail', accepted=False, score=None, error=str(error))
        (output / 'error.log').write_text(traceback.format_exc())
    result['completed_utc'] = datetime.now(timezone.utc).isoformat()
    result['submission_deadline_date'] = '2027-01-18'
    result['deadline_date_not_passed'] = datetime.now(timezone.utc).date().isoformat() <= '2027-01-18'
    result['official_submission_ready'] = False
    result['remaining_external_steps'] = ['Publish complete source under the checked license',
                                         'Review latest official rules and submit through the official process by its deadline']
    write_json(result_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description='Independent protocol-emulator verifier. Fast passes are never official acceptance.')
    parser.add_argument('candidate', nargs='?', default='candidates/tempo')
    parser.add_argument('--mode', choices=('fast', 'full'), default='fast')
    parser.add_argument('--seed', type=int, default=20260928)
    parser.add_argument('--out', type=Path, help='New output directory (must not already exist)')
    parser.add_argument('--score-file', type=Path, help='Atomically replace this score artifact, including on failures')
    parser.add_argument('--physical-timeout', type=int, default=14400)
    parser.add_argument('--compiler-workers', type=int, choices=(1, 2, 4), default=1,
                        help='Organizer-controlled compiler concurrency; private tests remain fresh')
    parser.add_argument('--physical-schedule', choices=('parallel', 'serial'), default='parallel',
                        help='Organizer-controlled scheduling; every acceptance check remains mandatory')
    args = parser.parse_args()
    output = args.out or ROOT / '.runs' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + secrets.token_hex(4))
    # Invalidate stale score before setup or validation can fail.
    if args.score_file:
        write_json(args.score_file, {'status': 'running', 'accepted': False, 'score': None})
    try:
        result = evaluate(args.candidate, output, mode=args.mode, seed=args.seed, physical_timeout=args.physical_timeout, physical_schedule=args.physical_schedule, compiler_workers=args.compiler_workers)
    except Exception as error:
        result = {'status': 'fail', 'accepted': False, 'score': None, 'error': str(error)}
    if args.score_file:
        write_json(args.score_file, result)
    print('Result:', result['status'], '| accepted:', result['accepted'], '| score:', result.get('score'))
    if result.get('error'):
        print(result['error'], file=sys.stderr)
    print('Evidence:', str(Path(output).resolve() / 'result.json'))
    return 0 if result['status'] == 'pass' else (2 if result['status'] == 'blocked' else 1)
