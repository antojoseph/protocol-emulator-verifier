"""Transport-independent Tempo host driver.

Supply exchange(command, nibble) -> reply_nibble. A physical transport must hold
the payload stable, toggle request, synchronize and await matching acknowledge.
Every method here completes each exchange before starting the next one.
"""
from __future__ import annotations
from collections.abc import Callable, Iterable


class Host:
    def __init__(self, exchange: Callable[[int, int], int]):
        self.exchange = exchange

    def address(self, register: int, engine: int = 0) -> None:
        if engine not in (0, 1) or not 0 <= register < 128:
            raise ValueError("engine must be 0/1 and register must be 0..127")
        address = register | engine << 7
        self.exchange(0, address & 15)
        self.exchange(1, address >> 4)

    def write(self, register: int, value: int, engine: int = 0) -> None:
        if not 0 <= value <= 255:
            raise ValueError("write value must fit a byte")
        self.address(register, engine)
        self.exchange(2, value & 15)
        self.exchange(3, value >> 4)

    def read(self, register: int, engine: int = 0) -> int:
        self.address(register, engine)
        return self.exchange(4, 0) | self.exchange(5, 0) << 4

    def configure(self, *, engine: int = 0, ownership: int = 0,
                  open_drain: int = 0, divider: int = 0, timeout: int = 0) -> None:
        if not 0 <= divider <= 65535 or not 0 <= timeout <= 65535:
            raise ValueError("divider and timeout must fit 16 bits")
        self.write(0, 0, engine)
        for reg, value in ((8, ownership), (9, open_drain), (10, divider & 255),
                           (11, divider >> 8), (12, timeout & 255), (13, timeout >> 8)):
            self.write(reg, value, engine)

    def load(self, words: Iterable[int], *, engine: int = 0, address: int = 0) -> None:
        words = list(words)
        if not 0 <= address < 64 or address + len(words) > 64:
            raise ValueError("program must fit memory")
        if any(not 0 <= word < 1 << 24 for word in words):
            raise ValueError("instruction must fit 24 bits")
        self.write(0, 0, engine)
        self.write(2, address, engine)
        for word in words:
            for reg, value in ((3, word & 255), (4, word >> 8 & 255), (5, word >> 16)):
                self.write(reg, value, engine)

    def restart(self, engine: int = 0, *, run: bool = True) -> None:
        """Restart flushes both FIFOs: preload TX only AFTER calling this."""
        self.write(0, 2 | int(run), engine)

    def start(self, engine: int = 0) -> None:
        self.write(0, 1, engine)

    def enqueue(self, value: int, engine: int = 0) -> None:
        if self.read(6, engine) == 4:
            raise BufferError("TX FIFO full")
        self.write(6, value, engine)

    def dequeue(self, engine: int = 0) -> int:
        if self.read(0x1B, engine) == 0:
            raise BufferError("RX FIFO empty")
        return self.read(7, engine)

    def capture(self, engine: int = 0) -> tuple[int, int]:
        pins = self.read(0x10, engine)
        timestamp = self.read(0x11, engine) | self.read(0x12, engine) << 8
        return timestamp, pins
