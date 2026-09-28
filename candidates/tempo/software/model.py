"""Independent, edge-stepped executable model of one Tempo execution engine.

Inputs pins/events are already synchronized; FIFO queues are modelled as the
host-visible depth-four byte queues. Edge returns side effects for a wrapper to
combine across engines. Deliberately no HDL dependency.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .assembler import decode


@dataclass
class Engine:
    program: dict[int, int] = field(default_factory=dict)
    divider: int = 0
    timeout: int = 0
    run: bool = False
    pc: int = 0
    x: int = 0
    y: int = 0
    sr: int = 0
    pin_out: int = 0
    pin_dir: int = 0
    halted: bool = False
    fault: int = 0
    fault_pc: int = 0
    fault_time: int = 0
    cycle: int = 0
    capture: int = 0
    capture_valid: bool = False
    phase: int = 0
    delay: int = 0
    failures: int = 0
    tx: list[int] = field(default_factory=list)
    rx: list[int] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.run and not self.halted and not self.fault

    def restart(self) -> None:
        program, divider, timeout, run = self.program, self.divider, self.timeout, self.run
        self.__dict__.update(Engine(program=program, divider=divider, timeout=timeout, run=run).__dict__)

    def load(self, words: list[int], address: int = 0) -> None:
        if self.run:
            raise ValueError("stop engine before programming")
        if address < 0 or address + len(words) > 64:
            raise ValueError("program exceeds memory")
        self.program.update(enumerate(words, address))

    def fail(self, code: int) -> None:
        self.fault = code
        self.fault_pc = self.pc
        self.fault_time = self.cycle
        self.pin_dir = 0

    def edge(self, pins: int = 0, events: int = 0, *, ena: bool = True,
             restart: bool = False, external_fault: bool = False) -> dict[str, int]:
        """Apply one external rising edge, returning FIFO/event request pulses."""
        effects = {"set": 0, "clear": 0, "pop": 0, "push": 0}
        if restart:
            self.restart()
            return effects
        if not ena:
            return effects
        old_cycle = self.cycle
        self.cycle = (self.cycle + 1) & 65535
        if not self.active:
            return effects
        if external_fault:
            self.fail(4)
            self.fault_time = old_cycle
            return effects
        if self.phase:
            self.phase -= 1
            return effects
        self.phase = self.divider
        if self.delay:
            self.delay -= 1
            return effects
        if self.pc not in self.program:
            self.fail(1)
            self.fault_time = old_cycle
            return effects
        op, arg, operand, delay = decode(self.program[self.pc])
        next_pc = (self.pc + 1) & 63
        blocked = False
        illegal = False
        if op == 0:
            pass
        elif op == 1:
            if arg < 5:
                setattr(self, ("pin_out", "pin_dir", "x", "y", "sr")[arg], operand)
            elif arg == 5:
                self.pin_out |= operand
            elif arg == 6:
                self.pin_out &= ~operand & 255
            elif arg == 7:
                self.pin_dir |= operand
            elif arg == 8:
                self.pin_dir &= ~operand & 255
            else:
                illegal = True
        elif op == 2:
            mask = 1 << (arg & 7)
            bit = self.sr >> 7 if arg & 8 else self.sr & 1
            self.pin_out = (self.pin_out & ~mask) | bit * mask
            self.sr = (self.sr << 1 & 255) if arg & 8 else self.sr >> 1
        elif op == 3:
            bit = pins >> (arg & 7) & 1
            if operand & 1:
                self.sr = (self.sr & 254) | bit if arg & 8 else (self.sr & 127) | bit << 7
            else:
                self.sr = ((self.sr << 1) | bit) & 255 if arg & 8 else (self.sr >> 1) | bit << 7
        elif op == 4:
            conditions = [True, self.x != 0, self.y != 0, self.sr == 0, self.sr != 0,
                          self.x == 0, self.y == 0, bool(events & 1), bool(events & 2)]
            if arg > 8:
                illegal = True
            elif conditions[arg]:
                next_pc = operand & 63
                if arg == 1:
                    self.x -= 1
                elif arg == 2:
                    self.y -= 1
        elif op == 5:
            blocked = ((pins >> (arg & 7)) & 1) != (arg >> 3)
        elif op == 6:
            blocked = not self.tx
            if not blocked:
                self.sr = self.tx.pop(0)
                effects["pop"] = 1
        elif op == 7:
            blocked = len(self.rx) == 4
            if not blocked:
                self.rx.append(self.sr)
                effects["push"] = 1
        elif op == 8:
            if arg == 0: self.sr = self.x
            elif arg == 1: self.sr = self.y
            elif arg == 2: self.x = self.sr
            elif arg == 3: self.y = self.sr
            elif arg == 4: self.sr ^= operand
            elif arg == 5: self.sr &= operand
            elif arg == 6: self.sr |= operand
            elif arg == 7: self.sr = (self.sr + operand) & 255
            elif arg == 8: self.sr ^= 255
            else: illegal = True
        elif op == 9:
            if arg < 2:
                effects["set"] = 1 << arg
            elif arg < 4:
                effects["clear"] = 1 << (arg - 2)
            elif arg in (8, 9):
                mask = 1 << (arg - 8)
                blocked = not events & mask
                if not blocked:
                    effects["clear"] = mask
            else:
                illegal = True
        elif op == 10:
            self.capture = old_cycle << 8 | pins
            self.capture_valid = True
        elif op == 15:
            self.halted = True
            self.pin_dir = 0
        else:
            illegal = True
        if illegal:
            self.fail(3)
            self.fault_time = old_cycle
        elif blocked:
            self.failures = min(self.failures + 1, 65535)
            if self.timeout and self.failures >= self.timeout:
                self.fail(2)
                self.fault_time = old_cycle
        else:
            self.failures = 0
            self.pc = next_pc
            self.delay = delay
        return effects
