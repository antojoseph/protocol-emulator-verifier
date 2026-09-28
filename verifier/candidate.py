"""Validate an untrusted candidate before compiling any of it."""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from .common import ROOT, VerificationError, digest, read_json, within

MAX_BYTES = 8 * 1024 * 1024
MAX_FILES = 256
PORTS = {'ui_in': ('input', 8), 'uo_out': ('output', 8),
         'uio_in': ('input', 8), 'uio_out': ('output', 8), 'uio_oe': ('output', 8),
         'clk': ('input', 1), 'rst_n': ('input', 1), 'ena': ('input', 1)}


def verilog_code_for_checks(source):
    """Remove real comments/string contents without confusing their delimiters.

    A quote starts a string only outside an escaped identifier. Strings consume
    escaped quotes/backslashes, and escaped identifiers consume every character
    through whitespace, including slash, quote, dollar and backtick characters.
    Keeping escaped identifiers in the audit is intentionally conservative: they
    cannot conceal forbidden attribute/system names or preprocessor spellings.
    Newlines are retained so deleting a comment never joins adjacent tokens.
    """
    output = []
    index = 0
    size = len(source)

    def blank(text):
        return ''.join('\n' if char == '\n' else ' ' for char in text)

    while index < size:
        start = index
        if source.startswith('//', index):
            end = source.find('\n', index+2)
            index = size if end == -1 else end
            output.append(blank(source[start:index]))
        elif source.startswith('/*', index):
            end = source.find('*/', index+2)
            if end == -1:
                raise VerificationError('Unterminated Verilog block comment')
            index = end+2
            output.append(blank(source[start:index]))
        elif source[index] == '"':
            index += 1
            while index < size:
                if source[index] == '\\':
                    index += 2
                elif source[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            else:
                raise VerificationError('Unterminated Verilog string')
            if index > size:
                raise VerificationError('Unterminated Verilog string escape')
            output.append(blank(source[start:index]))
        elif source[index] == '\\':
            index += 1
            while index < size and not source[index].isspace():
                index += 1
            output.append(source[start:index])
        else:
            output.append(source[index])
            index += 1
    return ''.join(output)


def validate(candidate):
    candidate = Path(candidate).resolve()
    files = []
    total = 0
    for path in candidate.rglob('*'):
        if path.is_symlink():
            raise VerificationError('Candidate symlinks are forbidden')
        if not path.is_file():
            continue
        relative = path.relative_to(candidate)
        if any(p.startswith('.') or p == '__pycache__' for p in relative.parts):
            continue
        if path.suffix == '.pyc':
            continue
        total += path.stat().st_size
        files.append(relative.as_posix())
    if not files or len(files) > MAX_FILES or total > MAX_BYTES:
        raise VerificationError('Candidate exceeds file/byte budget or is empty')
    manifest = read_json(within(candidate, 'candidate.json'))
    if manifest.get('schema_version') != 1:
        raise VerificationError('Unsupported candidate schema_version')
    top = manifest.get('top_module', '')
    if not re.fullmatch(r'tt_um_[A-Za-z0-9_]+', top):
        raise VerificationError('top_module must be a Tiny Tapeout tt_um_ identifier')
    if not isinstance(manifest.get('name'), str) or len(manifest['name']) > 100:
        raise VerificationError('Missing/invalid candidate name')
    rtl = manifest.get('rtl_files')
    if not isinstance(rtl, list) or not rtl or len(rtl) > 64 or len(set(rtl)) != len(rtl):
        raise VerificationError('rtl_files must contain 1..64 unique paths')
    for name in rtl:
        path = within(candidate, name)
        if path.suffix not in ('.v', '.sv'):
            raise VerificationError('RTL must be Verilog/SystemVerilog source')
        original = path.read_text()
        source = verilog_code_for_checks(original)
        if re.search(r'\b(?:initial|final|specify|force|release|bind|program|primitive)\b', source):
            raise VerificationError('Unsupported simulation/initialization construct in ' + name)
        if re.search(r'\b(?:translate_off|synthesis_off|blackbox|whitebox)\b', original, re.I):
            raise VerificationError('Synthesis exclusions/black boxes forbidden in ' + name)
        directives = re.findall(r'`\s*([A-Za-z_][A-Za-z0-9_]*)', source)
        if any(d not in ('default_nettype', 'timescale', 'resetall') for d in directives):
            raise VerificationError('Unsupported preprocessor directive in ' + name)
        calls = re.findall(r'\$([A-Za-z_][A-Za-z0-9_]*)', source)
        if any(c not in ('clog2', 'signed', 'unsigned', 'bits', 'size') for c in calls):
            raise VerificationError('Simulation system task/function forbidden in ' + name)
        if re.search(r'#\s*(?:[0-9]|\(\s*[0-9]+\s*\)\s*;)', source):
            raise VerificationError('Simulation delays forbidden in ' + name)
    within(candidate, manifest.get('adapter'))
    license_path = within(candidate, manifest.get('license_file'))
    license_text = ' '.join(license_path.read_text().split())
    if manifest.get('license') != 'Apache-2.0':
        raise VerificationError('This verifier version supports Apache-2.0; other open-source licenses require a policy update')
    canonical = ' '.join((ROOT / 'verifier/policy/Apache-2.0.txt').read_text().split())
    if license_text != canonical:
        raise VerificationError('License text does not match the declared Apache-2.0 license')
    notice = within(candidate, manifest.get('attribution_file'))
    if not notice.read_text().strip():
        raise VerificationError('Attribution NOTICE is empty')
    return manifest, {name: digest(candidate / name) for name in sorted(files)}


def snapshot(candidate, destination, hashes):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    for name, expected in hashes.items():
        source = within(candidate, name)
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        if digest(target) != expected:
            raise VerificationError('Candidate changed while snapshotting: ' + name)
    return destination


def check_ports(module):
    ports = module.get('ports', {})
    if set(ports) != set(PORTS):
        raise VerificationError('Top-level port set differs from Tiny Tapeout digital interface')
    for name, (direction, width) in PORTS.items():
        port = ports[name]
        if port.get('direction') != direction or len(port.get('bits', [])) != width:
            raise VerificationError('Top-level port direction/width mismatch: ' + name)
