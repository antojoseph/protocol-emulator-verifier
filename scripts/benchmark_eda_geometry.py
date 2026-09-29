"""Diagnostic only: deterministic in-memory KLayout geometry; no verdicts."""
import hashlib
import json
import pathlib
import resource
import time
import klayout.db as db

def cpu_stats():
    return {k: int(v) for k, v in (line.split() for line in pathlib.Path('/sys/fs/cgroup/cpu.stat').read_text().splitlines())}

def run():
    a, b = db.Region(), db.Region()
    for y in range(200):
        for x in range(200):
            px, py = x * 50, y * 50
            a.insert(db.Box(px, py, px + 40, py + 40))
            b.insert(db.Box(px + 17, py + 19, px + 58, py + 61))
    stats = []
    for trial in range(4):
        before = cpu_stats()
        start, cpu = time.perf_counter(), time.process_time()
        checksum = []
        for _ in range(3):
            c = (a ^ b).merged()
            d = c.sized(3).merged()
            e = d - a
            checksum.append([c.size(), c.area(), d.size(), d.area(), e.size(), e.area()])
        after = cpu_stats()
        stats.append({'trial': trial, 'wall_seconds': time.perf_counter() - start,
                      'process_cpu_seconds': time.process_time() - cpu,
                      'checksum': hashlib.sha256(json.dumps(checksum).encode()).hexdigest(),
                      'cgroup_delta': {k: after[k] - before[k] for k in before}})
    maps = pathlib.Path('/proc/self/maps').read_text()
    print(json.dumps({'klayout_version': db.__version__, 'cpu_max': pathlib.Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
                      'rosetta_in_maps': 'rosetta' in maps.lower(),
                      'max_rss_kib': resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                      'trials': stats}, indent=2), flush=True)

if __name__ == '__main__':
    run()
