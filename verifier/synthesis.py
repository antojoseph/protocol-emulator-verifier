from __future__ import annotations

import json
from pathlib import Path
from .candidate import check_ports
from .common import VerificationError, read_json, run, tool
from .containers import run_eda


def run_synthesis(candidate, manifest, output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    netlist = output / 'generic.json'
    sources = ' '.join(json.dumps(str(Path(candidate) / p)) for p in manifest['rtl_files'])
    script = output / 'synth.ys'
    script.write_text('read_verilog -sv ' + sources + '\n' +
                      'hierarchy -check -top ' + manifest['top_module'] + '\n' +
                      'synth -top ' + manifest['top_module'] + '\n' +
                      'check -assert\nwrite_json ' + json.dumps(str(netlist)) + '\n')
    ok, detail = run_eda(['yosys', '-Q', '-s', str(script)], cwd=output,
                         readonly=[candidate], log_path=output / 'synthesis.log', timeout=180)
    if not ok or not netlist.is_file():
        raise VerificationError('Synthesis failed; see synthesis/synthesis.log')
    modules = read_json(netlist)['modules']
    check_ports(modules[manifest['top_module']])
    # Count flattened primitives, including multiplicity of child instances.
    counts = {}
    def count(name, stack=()):
        if name in stack:
            raise VerificationError('Recursive hardware hierarchy')
        if name in counts:
            return counts[name]
        module = modules[name]
        if any(key in module.get('attributes', {}) for key in ('blackbox', 'whitebox', 'lib_whitebox')):
            raise VerificationError('Unresolved black-box module')
        total = 0
        for cell in module.get('cells', {}).values():
            kind = cell['type']
            if kind in modules:
                total += count(kind, stack + (name,))
            elif kind.startswith('$_'):
                if 'LATCH' in kind or 'TBUF' in kind:
                    raise VerificationError('Latches/internal tristates unsupported in this benchmark version')
                total += 1
            else:
                raise VerificationError('Unmapped hardware cell: ' + kind)
        counts[name] = total
        return total
    cells = count(manifest['top_module'])
    if cells <= 0:
        raise VerificationError('Empty synthesized design')
    return {'status': 'pass', 'checks': [{'name': 'synthesis_and_tt_ports', 'status': 'pass'}],
            'metrics': {'generic_cells': cells, 'provisional_score': 1_000_000 / cells},
            'artifacts': {'generic_netlist': str(netlist)}}
