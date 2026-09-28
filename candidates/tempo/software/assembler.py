#!/usr/bin/env python3
"""Tempo's dependency-free 24-bit assembler, also usable as a Python library.

Text format: LABEL:, MNEMONIC arg, operand @ delay. Registers/conditions have
symbolic names; integers accept 0x/0b prefixes. # starts a comment. Only JMP
operands accept labels. Example: OUT msb0 @ 14; JMP xnz, loop.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import re

OPCODES = {name: code for code, name in enumerate(
    ["NOP", "SET", "OUT", "IN", "JMP", "WAIT", "PULL", "PUSH", "ALU", "EVENT", "CAPTURE"])}
OPCODES["HALT"] = 15
ARGUMENTS = {
    "SET": {"pins": 0, "dirs": 1, "x": 2, "y": 3, "sr": 4,
            "setpins": 5, "clearpins": 6, "setdirs": 7, "cleardirs": 8},
    "JMP": {"always": 0, "xnz": 1, "ynz": 2, "zero": 3, "nonzero": 4,
            "xzero": 5, "yzero": 6, "event0": 7, "event1": 8},
    "ALU": {"from_x": 0, "from_y": 1, "to_x": 2, "to_y": 3,
            "xor": 4, "and": 5, "or": 6, "add": 7, "not": 8},
    "EVENT": {"set0": 0, "set1": 1, "clear0": 2, "clear1": 3,
              "wait0": 8, "wait1": 9},
    "OUT": {**{f"lsb{i}": i for i in range(8)}, **{f"msb{i}": i+8 for i in range(8)}},
    "IN": {**{f"right{i}": i for i in range(8)}, **{f"left{i}": i+8 for i in range(8)}},
    "WAIT": {**{f"low{i}": i for i in range(8)}, **{f"high{i}": i+8 for i in range(8)}},
}


def encode(opcode: str | int, argument: int = 0, operand: int = 0, delay: int = 0) -> int:
    """Encode without silently truncating an out-of-range field."""
    op = OPCODES[opcode.upper()] if isinstance(opcode, str) else opcode
    for name, value, maximum in (("opcode", op, 15), ("argument", argument, 15),
                                  ("operand", operand, 255), ("delay", delay, 255)):
        if not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"{name} must be an integer in 0..{maximum}: {value!r}")
    return op << 20 | argument << 16 | operand << 8 | delay


def decode(word: int) -> tuple[int, int, int, int]:
    if not 0 <= word < 1 << 24:
        raise ValueError("instruction must fit 24 bits")
    return word >> 20, word >> 16 & 15, word >> 8 & 255, word & 255


def number(value: str, symbols: dict[str, int] | None = None) -> int:
    if symbols and value in symbols:
        return symbols[value]
    return int(value, 0)


@dataclass(frozen=True)
class Assembly:
    words: list[int]
    labels: dict[str, int]
    source: str

    def hex(self) -> str:
        return "".join(f"{word:06x}\n" for word in self.words)


def assemble(source: str) -> Assembly:
    labels: dict[str, int] = {}
    lines: list[tuple[int, str]] = []
    for lineno, raw in enumerate(source.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" in line:
            label, line = line.split(":", 1)
            label = label.strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", label) or label in labels:
                raise ValueError(f"line {lineno}: invalid or duplicate label {label!r}")
            labels[label] = len(lines)
            line = line.strip()
        if line:
            lines.append((lineno, line))
    if len(lines) > 64:
        raise ValueError(f"program uses {len(lines)} words; engine capacity is 64")
    words = []
    for lineno, line in lines:
        try:
            instruction, *delay_text = line.split("@")
            if len(delay_text) > 1:
                raise ValueError("multiple delay fields")
            delay = number(delay_text[0].strip()) if delay_text else 0
            parts = instruction.replace(",", " ").split()
            mnemonic = parts[0].upper()
            if len(parts) > 3:
                raise ValueError("too many operands")
            argument = number(parts[1], ARGUMENTS.get(mnemonic)) if len(parts) > 1 else 0
            operand = number(parts[2], labels if mnemonic == "JMP" else None) if len(parts) > 2 else 0
            if mnemonic == "JMP" and not 0 <= operand < 64:
                raise ValueError("jump destination must be 0..63")
            words.append(encode(mnemonic, argument, operand, delay))
        except (ValueError, KeyError) as error:
            raise ValueError(f"line {lineno}: {line}: {error}") from error
    return Assembly(words, labels, source)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    result = assemble(args.source.read_text())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result.hex())
    else:
        print(result.hex(), end="")


if __name__ == "__main__":
    main()
