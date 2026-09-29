#!/usr/bin/env python3
"""Summarize completed trusted result metadata; never inspect private stimuli."""
import argparse
from datetime import datetime
import json
import hashlib
from pathlib import Path



def elapsed(result):
    return (datetime.fromisoformat(result['completed_utc']) -
            datetime.fromisoformat(result['started_utc'])).total_seconds()


def layout_hash(path):
    """Ignore only GDS library/structure timestamps for a historical comparison.

    Acceptance itself compares unmodified bytes; this is reporting only.
    """
    h = hashlib.sha256()
    records = 0
    with Path(path).open('rb') as stream:
        while True:
            header = stream.read(4)
            if not header:
                break
            if len(header) != 4:
                raise ValueError('Truncated GDS header')
            length = int.from_bytes(header[:2], 'big')
            if length < 4 or length % 2:
                raise ValueError('Invalid GDS record length')
            body = stream.read(length-4)
            if len(body) != length-4:
                raise ValueError('Truncated GDS record')
            if header[2] in (1,5):
                if len(body) != 24 or header[3] != 2:
                    raise ValueError('Unexpected GDS timestamp record')
                body = bytes(24)
                records += 1
            h.update(header + body)
    return {'sha256_ignoring_timestamps': h.hexdigest(), 'timestamp_records': records}


def summarize(path):
    r = json.loads(Path(path).read_text())
    if r.get('accepted') is not True or r.get('status') != 'pass':
        raise ValueError('A complete accepted full run is required')
    physical = r['stages']['physical']
    commands = physical['provenance']['commands']
    timings = {c['stage']: c['elapsed_seconds'] for c in commands}
    stages = {c['stage']: c for c in commands}
    h, p = stages['harden'], stages['official_precheck']
    overlap = max(0, min(h['started_monotonic'] + h['elapsed_seconds'],
                         p['started_monotonic'] + p['elapsed_seconds'])
                     - max(h['started_monotonic'], p['started_monotonic']))
    flow_times = {}
    for runtime in (Path(path).resolve().parent/'physical/runs/authoritative').glob('*/runtime.txt'):
        hours, minutes, seconds = runtime.read_text().strip().split(':')
        flow_times[runtime.parent.name] = int(hours)*3600 + int(minutes)*60 + float(seconds)
    return {'accepted': r['accepted'], 'score': r['score'],
            'candidate_sha256': r['candidate']['source_sha256'],
            'harness_sha256': r['harness_sha256'],
            'elapsed_seconds': elapsed(r),
            'physical_seconds': physical['elapsed_seconds'],
            'physical_metrics': physical['metrics'],
            'physical_checks': physical['checks'],
            'artifact_sha256': {k:v['sha256'] for k,v in physical['artifacts'].items()},
            'layout_comparison_hash': layout_hash(physical['artifacts']['gds']['path']),
            'commands_seconds': timings,
            'flow_steps_seconds': dict(sorted(flow_times.items())),
            'harden_precheck_overlap_seconds': round(overlap, 6),
            'execution': physical['provenance']['execution'],
            'stages': {k: {'status': v['status'], 'metrics': v.get('metrics')} for k,v in r['stages'].items()
                       if k != 'physical'}}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('result', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    report = summarize(args.result)
    for name in ('host-load-observation', 'container-overlap-audit'):
        observation = args.result.parent / (name + '.json')
        if observation.is_file():
            report[name.replace('-', '_')] = json.loads(observation.read_text())
    if args.baseline:
        old = json.loads(args.baseline.read_text())
        assert old['accepted'] is True
        old_physical = old['stages']['physical']
        report['historical_comparison'] = {
            'baseline_elapsed_seconds': elapsed(old),
            'minutes_saved': (elapsed(old)-report['elapsed_seconds'])/60,
            'speedup': elapsed(old)/report['elapsed_seconds'],
            'candidate_identical': old['candidate']['source_sha256']==report['candidate_sha256'],
            'score_identical': old['score']==report['score'],
            'physical_metrics_identical': old_physical['metrics']==report['physical_metrics'],
            'artifact_bytes_identical': {k:v['sha256']==report['artifact_sha256'][k]
                                         for k,v in old_physical['artifacts'].items()},
            'gds_identical_except_timestamps': layout_hash(old_physical['artifacts']['gds']['path'])==report['layout_comparison_hash'],
            'limitation': 'One historical run and one new run; not a randomized repeated performance study.'}
    args.out.write_text(json.dumps(report, indent=2, allow_nan=False)+'\n')
