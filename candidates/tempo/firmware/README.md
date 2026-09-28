# Programmable protocol examples

All seven programs are writable instructions loaded through the host interface
after reset. No UART, SPI or I2C state machine exists in RTL. The generated `.asm`
is readable source, `.hex` is one 24-bit word per line, and `manifest.json` records
pin ownership, electrical mode, divider, timeout and labels.

From the project directory:

```sh
python3 firmware/generate.py
python3 -m software.assembler firmware/spi_mode0.asm -o /tmp/spi.hex
make -C test
```

`@ N` means N additional core ticks after the executing tick. At external clock
frequency F and divider D, one tick lasts `(D + 1) / F` seconds. These defaults
assume **50 MHz**; they scale with the actual external clock.

| Program | Words | Pins | Default divider | Timing at 50 MHz |
|---|---:|---|---:|---|
| `uart_tx` | 9 | TX 0 | 26 | 16 ticks/bit; 432 external clocks, 8.64 µs, 115,740.74 baud |
| `uart_rx` | 13 | RX 0 | 26 | Same nominal bit period; tested with an asynchronous approximately 115,200-baud source |
| `spi_mode0` | 13 | MOSI 0, SCK 1, MISO 2, CS_n 3 | 1 | 18 ticks/bit, 36 clocks, 1.388889 MHz; high and low each 18 clocks |
| `i2c_write` | 29 | SDA 0, SCL 1 | 31 | Ordinary unstretched bits: 26 ticks, approximately 60.096 kHz |
| `i2c_read` | 46 | SDA 0, SCL 1 | 31 | Read-data bits: 22 ticks, approximately 71.023 kHz; address uses write-bit timing |
| `edge_capture` | 5 | input 0 | 0 | Timestamp unit 20 ns; synchronized input sampled one instruction after successful WAIT |
| `event_pulse` | 5 | output 7 | 0 | Event consumer drives high for nine clocks (180 ns), then releases |

The ordinary-bit I2C figures exclude byte boundaries, ACK, START, STOP and clock
stretching. The default tick is 640 ns. Write-bit SCL is high for at least 9 ticks
(5.76 µs) and low for at least 17 ticks (10.88 µs); read-bit high/low intervals
are at least 13/9 ticks (8.32/5.76 µs). START hold is at least 8 ticks (5.12 µs),
and STOP setup at least 9 ticks (5.76 µs). These are digital timing budgets;
external pull-ups and actual bus capacitance determine rise time.

The pin tests execute UART TX/RX, SPI and I2C write at these default dividers and
50 MHz. Additional divider-zero tests accelerate boundary and error cases.
No fabricated chip or FPGA measurement is implied.

## Host use

`software.host.Host` encodes the address/write/read nibble transactions. Supply a
transport `exchange(command, nibble) -> reply_nibble` that holds bundled command
and data stable before toggling request, then waits for the synchronized matching
acknowledgment. The transport depends on your microcontroller, FPGA or test jig;
this repository provides the protocol encoder and a working simulation host.

```python
from pathlib import Path
from software.assembler import assemble
from software.host import Host

host = Host(exchange)
host.configure(ownership=3, open_drain=3, divider=31, timeout=4096)
host.load(assemble(Path("firmware/i2c_write.asm").read_text()).words)
host.restart(run=False)          # restart FLUSHES both FIFOs
for byte in [1, 0xA0, 0x36]:     # two bytes: address 0xA0, payload 0x36
    host.enqueue(byte)
host.start()                    # resume without flushing
# Poll RX occupancy or service it from your host loop, then read two ACK results.
```

For UART TX and SPI, enqueue ordinary bytes. SPI returns one received byte for
each transmitted byte, with CS_n asserted around each byte. The SPI program uses
OUT MSB followed by IN merge into bit 0, so the same shift register exchanges a
whole byte in both directions without losing transmitted bits.

For I2C write, enqueue `transmitted_byte_count - 1`, then an address byte including
R/W=0, followed by data. An RX result of 0 means ACK and 1 means NACK. The program
emits STOP immediately on any NACK and leaves unsent bytes in TX. For I2C read,
enqueue `read_byte_count - 1`, then an address byte including R/W=1. RX contains
the address ACK/NACK first, then received data bytes. The controller ACKs all
read bytes except the final byte, which it NACKs. An address NACK skips the reads
and emits STOP. Neither example implements repeated START or multi-controller
arbitration; those require different firmware.

Both I2C pins use ownership=3 and open-drain=3. A one releases the pin, and every
released SCL high is confirmed by WAIT on the physical synchronized input.
Timeout 4096 at divider 31 is 2.62144 ms of failed blocking observations, applying
to both pin waits and FIFO stalls. Timeout faults release the pads. The examples
are single-controller transactions and assume external pull-ups.

## Receive servicing and error behavior

UART RX samples each data bit once, starting 24 core ticks after the synchronized
start observation; it is not a majority-vote oversampling receiver. It separately
samples the stop bit, restores the received byte from Y, and enqueues only valid
frames. A low stop bit, including a break, sets shared event1 and halts until
restart. It does not autonomously discard a break and resynchronize. Event1 is
firmware's framing-error flag, distinct from the hardware fault register.

RX FIFO depth is four bytes. The host must drain it quickly enough for a UART
stream; this example has no RTS/CTS and no overrun detector. If PUSH blocks, later
serial frames can be missed. Default timeout zero permits indefinite blocking;
a finite timeout can turn a prolonged full FIFO into a hardware timeout fault,
but it also limits idle WAIT time. The tests verify four back-to-back frames and
a configured timeout when a fifth frame fills the blocked PUSH. SPI stalls with
CS_n high between transfers, and I2C stalls with SCL low at its FIFO operations.

To demonstrate cross-engine behavior, load `edge_capture` in engine 0 and
`event_pulse` in engine 1 with disjoint ownership masks. Global register
`0x7e = 0x73` restarts/runs both and clears events on the same edge. A rising pin0
edge captures all synchronized pins and the source engine's 16-bit counter,
then broadcasts event0. Engine 1 consumes it and drives pin7 high for nine clocks,
then HALT releases the pad. Use an external pull-down for a low idle level; the
falling edge after release depends on the pull-down and load. At
divider zero, the test observes seven external rising edges from a pin transition
halfway between clocks to the pulse, including the two input synchronizers and
instruction/event handoff. CAPTURE measures the software observation time, not
the unsynchronized analog edge; the 16-bit counter wraps every 1.31072 ms at
50 MHz.

USB, Ethernet, CAN, electrical transceivers and analog compliance are not part of
these firmware examples.
