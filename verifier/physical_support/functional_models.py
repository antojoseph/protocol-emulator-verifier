"""Adapt the IHP timing model to zero-delay Icarus simulation.

The original CMOS5L model uses $setuphold/$recrem delayed-signal arguments as the
ONLY drivers of delayed_CLK/D/etc. Icarus does not implement those arguments.
For functional simulation, strip specify blocks and connect each delayed signal
to its original input. Cell primitives, UDP truth tables and logic are unchanged.
This generated copy is NOT a timing model and must never be used for SDF signoff.
"""
from pathlib import Path
import hashlib
import json
import re


def adapt(source: Path, destination: Path) -> Path:
    source_bytes = source.read_bytes()
    original = source_bytes.decode()
    functional, timing_blocks = re.subn(r"\bspecify\b.*?\bendspecify\b", "", original, flags=re.S)
    alias_count = 0
    def aliases(match):
        nonlocal alias_count
        declaration = match.group(0)
        names = re.findall(r"\bdelayed_(\w+)\b", declaration)
        alias_count += len(names)
        return declaration + "\n" + "\n".join(f"assign delayed_{name} = {name};" for name in names)
    functional = re.sub(r"\bwire\s+(?:delayed_\w+\s*,\s*)*delayed_\w+\s*;", aliases, functional)
    banner = "// GENERATED FUNCTIONAL-ONLY MODEL: specify removed, delayed inputs directly connected.\n"
    destination.write_text(banner + functional)
    metadata = {"source": str(source), "sha256_source": hashlib.sha256(source_bytes).hexdigest(),
                "sha256_functional": hashlib.sha256((banner+functional).encode()).hexdigest(),
                "removed_specify_blocks": timing_blocks, "direct_delayed_signal_aliases": alias_count,
                "timing_simulation": False}
    destination.with_suffix(".json").write_text(json.dumps(metadata, indent=2) + "\n")
    return destination
