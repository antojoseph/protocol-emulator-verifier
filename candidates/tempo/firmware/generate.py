#!/usr/bin/env python3
"""Generate readable firmware, assembled images and machine-readable contracts."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from software.assembler import assemble

PROGRAMS = {
"uart_tx": dict(ownership=1, open_drain=0, divider=26, timeout=0,
    pins={"tx": 0}, description="8N1, LSB first; 16 core ticks/bit; TX FIFO stream",
    source="""
SET pins, 1
SET dirs, 1
next_byte:
PULL
SET x, 7
SET pins, 0 @ 15
bit:
OUT lsb0 @ 14
JMP xnz, bit
SET pins, 1 @ 15
JMP always, next_byte
"""),
"uart_rx": dict(ownership=0, open_drain=0, divider=26, timeout=0,
    pins={"rx": 0}, description="8N1 receiver, 16 ticks/bit; samples 1.5 bits after synchronized start; bad stop sets event1 and halts",
    source="""
next_byte:
SET x, 7
WAIT low0 @ 23
bit:
IN right0 @ 14
JMP xnz, bit
ALU to_y
SET sr, 0
IN left0
JMP zero, framing_error
ALU from_y
PUSH
JMP always, next_byte
framing_error:
EVENT set1
HALT
"""),
"spi_mode0": dict(ownership=11, open_drain=0, divider=1, timeout=0,
    pins={"mosi": 0, "sck": 1, "miso": 2, "cs_n": 3},
    description="Mode0 MSB-first full duplex, one CS pulse per byte, 18 ticks/bit; TX in / RX out",
    source="""
SET pins, 8
SET dirs, 11
next_byte:
PULL
SET x, 7
SET clearpins, 8 @ 3
bit:
OUT msb0 @ 3
SET setpins, 2 @ 5
IN left2, 1 @ 2
SET clearpins, 2 @ 3
JMP xnz, bit
SET setpins, 8 @ 3
PUSH
JMP always, next_byte
"""),
"i2c_write": dict(ownership=3, open_drain=3, divider=31, timeout=4096,
    pins={"sda": 0, "scl": 1},
    description="Single-controller write: TX count-minus-one then address/data bytes; RX one ACK bit/byte (0 ACK, 1 NACK); aborts on NACK; START/STOP and stretch-aware SCL",
    source="""
SET pins, 3
SET dirs, 3
PULL
ALU to_x
WAIT high1 @ 7
WAIT high0
SET clearpins, 1 @ 7
SET clearpins, 2 @ 7
next_byte:
PULL
SET y, 7
bit:
OUT msb0 @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
JMP ynz, bit
SET setpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 3
SET sr, 0
IN left0 @ 3
SET clearpins, 2 @ 7
PUSH
JMP nonzero, stop
JMP xnz, next_byte
stop:
SET clearpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 7
SET setpins, 1 @ 7
HALT
"""),
"i2c_read": dict(ownership=3, open_drain=3, divider=31, timeout=4096,
    pins={"sda": 0, "scl": 1},
    description="Single-controller read: TX read-count-minus-one then address byte with R/W=1; RX address ACK followed by data; controller ACKs all except final byte (NACK), aborts address NACK",
    source="""
SET pins, 3
SET dirs, 3
PULL
ALU to_x
PULL
SET y, 7
WAIT high1 @ 7
WAIT high0
SET clearpins, 1 @ 7
SET clearpins, 2 @ 7
address_bit:
OUT msb0 @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
JMP ynz, address_bit
SET setpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 3
SET sr, 0
IN left0 @ 3
SET clearpins, 2 @ 7
PUSH
JMP nonzero, stop
next_byte:
SET sr, 0
SET y, 7
read_bit:
SET setpins, 2
WAIT high1 @ 7
IN left0 @ 3
SET clearpins, 2 @ 7
JMP ynz, read_bit
PUSH
JMP xzero, last_byte
SET clearpins, 1
JMP always, acknowledge
last_byte:
SET setpins, 1
acknowledge:
NOP @ 7
SET setpins, 2
WAIT high1 @ 7
SET clearpins, 2 @ 7
SET setpins, 1
JMP xnz, next_byte
stop:
SET clearpins, 1 @ 7
SET setpins, 2
WAIT high1 @ 7
SET setpins, 1 @ 7
HALT
"""),
"edge_capture": dict(ownership=0, open_drain=0, divider=0, timeout=4096,
    pins={"edge_input": 0}, description="Wait for low then rising input; capture synchronized pads/cycle count, broadcast event0",
    source="""
WAIT low0
WAIT high0
CAPTURE
EVENT set0
HALT
"""),
"event_pulse": dict(ownership=128, open_drain=0, divider=0, timeout=4096,
    pins={"pulse_output": 7}, description="Wait/consume event0, emit nine-tick pulse on pin7, halt and release",
    source="""
SET pins, 0
SET dirs, 128
EVENT wait0
SET setpins, 128 @ 8
HALT
"""),
}


def main():
    destination = Path(__file__).resolve().parent
    manifest = {}
    for name, spec in PROGRAMS.items():
        source = "# " + spec["description"] + "\n" + spec["source"].strip() + "\n"
        program = assemble(source)
        (destination / f"{name}.asm").write_text(source)
        (destination / f"{name}.hex").write_text(program.hex())
        manifest[name] = {key: value for key, value in spec.items() if key != "source"}
        manifest[name]["words"] = len(program.words)
        manifest[name]["labels"] = program.labels
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Generated " + ", ".join(f"{name}: {data['words']} words" for name, data in manifest.items()))


if __name__ == "__main__":
    main()
