#!/usr/bin/env python3
"""Tempo's replaceable implementation of the public host-action contract.

Only JSON travels across the isolation boundary. The verifier owns protocol
peers, observations, assertions, random response bytes and the resulting score.
"""
from __future__ import annotations
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from software.assembler import assemble


class Transport:
    def __init__(self):
        self.request = 0
        self.actions = []

    def command(self, code, value):
        payload = code << 4 | value
        self.actions.append({"op": "drive", "value": self.request << 7 | payload, "cycles": 1})
        self.request ^= 1
        self.actions.append({"op": "drive", "value": self.request << 7 | payload, "cycles": 1})
        self.actions.append({"op": "wait", "mask": 16, "value": self.request << 4, "timeout": 100})

    def address(self, address):
        self.command(0, address & 15)
        self.command(1, address >> 4)

    def write(self, address, value):
        self.address(address)
        self.command(2, value & 15)
        self.command(3, value >> 4)

    def read(self, address, index):
        self.address(address)
        self.command(4, 0)
        self.actions.append({"op": "capture", "byte": index, "src_lsb": 0, "width": 4, "dst_lsb": 0})
        self.command(5, 0)
        self.actions.append({"op": "capture", "byte": index, "src_lsb": 0, "width": 4, "dst_lsb": 4})

    def take(self):
        actions, self.actions = self.actions, []
        return actions

    def load(self, words, ownership, drain=0, divider=0, tx=()):
        self.write(0, 0)
        for register, value in ((8, ownership), (9, drain), (10, divider), (11, 0), (12, 0), (13, 0), (2, 0)):
            self.write(register, value)
        for word in words:
            self.write(3, word & 255)
            self.write(4, word >> 8 & 255)
            self.write(5, word >> 16 & 255)
        self.write(0, 2)
        for value in tx:
            self.write(6, value)


def generic_program(program):
    # Input pin 0 triggers; input pin 1 selects the pulse width at run time.
    return assemble(f"""
SET pins, 0
SET dirs, 128
SET x, {program['count']-1}
WAIT low0
WAIT high0
SET sr, 0
IN left1
JMP zero, false_branch
true_branch:
SET setpins, 128 @ {program['width_true']-1}
SET clearpins, 128 @ {program['gap']-2}
JMP xnz, true_branch
JMP always, done
false_branch:
SET setpins, 128 @ {program['width_false']-1}
SET clearpins, 128 @ {program['gap']-2}
JMP xnz, false_branch
done:
JMP always, done
""").words


def compile_request(request):
    transport = Transport()
    kind = request["kind"]
    if kind == "programmable":
        programs = [(generic_program(program), 128, 0, 0, [], 0) for program in request["programs"]]
    else:
        filename, owner, drain, divider, tx, read_count = {
            "uart_tx": ("uart_tx", 1, 0, 26, request.get("tx", []), 0),
            "uart_rx": ("uart_rx", 0, 0, 26, [], request.get("count", 0)),
            "spi": ("spi_mode0", 11, 0, 1, request.get("tx", []), len(request.get("tx", []))),
            "i2c_write": ("i2c_write", 3, 3, 31,
                          [len(request.get("tx", []))-1]+request.get("tx", []), len(request.get("tx", []))),
            "i2c_read": ("i2c_read", 3, 3, 31,
                         [request.get("count", 1)-1, request.get("address", 0)*2+1], request.get("count", 0)+1),
        }[kind]
        words = assemble((ROOT / "firmware" / f"{filename}.asm").read_text()).words
        programs = [(words, owner, drain, divider, tx, read_count)]
    stages = []
    for words, owner, drain, divider, tx, read_count in programs:
        transport.load(words, owner, drain, divider, tx)
        setup = transport.take()
        transport.write(0, 1)
        start = transport.take()
        for index in range(read_count):
            transport.read(7, index)
        stages.append({"setup": setup, "start": start, "read": transport.take()})
    return {"stages": stages}


if __name__ == "__main__":
    request = json.load(sys.stdin)
    print(json.dumps(compile_request(request), separators=(",", ":")))
