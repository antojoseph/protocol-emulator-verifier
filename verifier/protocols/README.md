# External functional contract, version 1

These are explicit benchmark choices that exercise the competition's requested
reprogrammability and starting UART/SPI/I²C capabilities. The blog does not
prescribe the modes, rates, host interface, or finite test distribution below.
Passing is evidence for this contract, not universal protocol certification.

The candidate exposes the Tiny Tapeout digital interface and uses one unchanged
hardware implementation across every workload. The clock is 50 MHz. Protocol
pins have digital pull-ups in the trusted testbench. Reset must release them.

| Workload | Protocol pins | Required behavior |
|---|---|---|
| UART transmit | pin 0 TX | 8N1, LSB first, 432 clocks/bit; complete start/data/stop bit windows are checked |
| UART receive | pin 0 RX | 8N1 at approximately the same baud; privately selected bytes, asynchronous phase and small baud offset; return three bytes through host output pins |
| SPI controller | pin 0 MOSI, 1 SCK, 2 MISO, 3 CS# | mode 0, MSB first, 36–38 clocks/bit, approximately 50% duty cycle; one CS pulse per byte, exactly eight clocks; full duplex with privately selected response bytes |
| I²C controller write | pin 0 SDA, 1 SCL | Standard-mode, open drain; seven-bit address, up to two payload bytes; ACK/NACK, early termination, clock stretching, START/STOP, byte clock counts |
| I²C controller read | pin 0 SDA, 1 SCL | seven-bit address then two private response bytes; ACK continuation and NACK final byte, address NACK termination, stretching |
| Programmable response | pin 0 trigger, 1 branch input, 7 output | load a counted pulse program; after loading, the external peer selects a branch and triggers execution; check requested pulse widths, gaps and finite count; replace the program without resetting or rebuilding hardware and repeat |

I²C digital timing checks include Standard-mode SCL frequency ≤100 kHz, low time
≥4.7 µs, high time ≥4.0 µs, START hold ≥4.0 µs, STOP setup ≥4.0 µs, and data setup
≥250 ns (rounded up to 13 clocks). The peer stretches SCL for a privately selected
1500–2400 clocks. Analog rise/fall times, electrical drive strength, metastability,
multi-controller arbitration and repeated START are outside this digital suite.
They must not be advertised as verified. SPI modes 1–3 and other UART formats are
also outside version 1. Protocol extensions require a versioned contract change.

## Candidate adapter

The candidate owns its RTL, ISA, assembler/compiler, firmware and host encoding.
Its `adapter.py` receives one public JSON workload on stdin and emits one JSON
object on stdout. Run the adapter only through the runner's isolation boundary.
No candidate Python executes in the trusted simulator process.

Every request has `schema_version: 1`, `clock_hz: 50000000`, and `kind`:

- `uart_tx`: `tx` is a three-byte list and `bit_cycles` is 432.
- `uart_rx`: `count` is 3 and `bit_cycles` is 432. Incoming bytes are private.
- `spi`: `tx` is a three-byte list and `period_cycles` is 36. Incoming bytes are private.
- `i2c_write`: `tx` contains the already shifted write address followed by two data bytes; `bus_hz` is 100000. The ACK/NACK pattern is private.
- `i2c_read`: `address` is the unshifted seven-bit address, `count` is 2, and `bus_hz` is 100000. Address ACK/NACK and data are private.
- `programmable`: `programs` contains two objects with `count`, `width_false`, `width_true`, and `gap`, measured in external clock cycles. Trigger time and selected branch are private. Count is 2–5, false width 10–25, true width 30–50, gap 8–24. Output must initialize low within 128 clocks after launch and remain low throughout the armed interval; actively driven high pulses during initialization fail. First response must occur within 128 clocks after the trigger. After the exact pulse count, the output must stay low until the next program is loaded.

The reply is `{"stages":[{"setup": [...], "start": [...], "read": [...]}]}`.
There is one stage per request except the programmable request has two, sharing
the same running chip and host transport state. Setup loads the program and
application payload. Start launches it. **All host actions finish before the
trusted peer observes or drives the transaction. Host inputs remain constant
throughout the timed protocol operation.** Read transfers completed results.

Only these action objects are accepted:

```json
{"op":"drive","value":128,"cycles":1}
{"op":"wait","mask":16,"value":16,"timeout":100}
{"op":"capture","byte":0,"src_lsb":0,"width":4,"dst_lsb":0}
```

`drive` sets only `ui_in` on a falling clock edge and holds it for 1–128 cycles.
`wait` observes `(uo_out & mask) == value`, with a 1–128 cycle deadline.
`capture`, allowed only in `read`, copies the specified `uo_out` bits to a result
byte. No constants, expressions, hierarchical paths, branches, protocol-pin
driving, or arbitrary HDL are allowed. Every requested result byte must have all
eight destination bits captured exactly once. Unknown signal values fail.

Read counts are zero for TX and programmable workloads, three for UART RX/SPI,
the length of `tx` for I²C writes, and `count + 1` for I²C reads. I²C write results
are one ACK status per transmitted byte (0 ACK, 1 NACK). I²C read results begin
with address ACK status then data. Following a NACK, later read slots are ignored;
the adapter must still supply the full read action sequence, without knowing
where the target NACKed. An implementation may expose deterministic empty-slot
values on host reads.

The parser rejects extra JSON fields and malformed ranges. Each response is
limited to 20000 actions and 1500000 worst-case host clocks. Independently, each
case must finish within 200000 clock cycles, and compile/simulation subprocesses
have wall-time and log-size limits. Faster loaders and faster internal execution
are permitted within the observable timing contract.

## Trust and evidence

Public workload generation is controlled by the CLI seed. After **all** adapter
processes have returned, the verifier generates a fresh 256-bit private seed for
response data, ACK/NACK distribution, timing and runtime branches. Private inputs
are never sent to the adapter. `private_replay.json` allows a trusted operator to
reproduce a failure; a normal new run generates new private inputs. Candidate
processes must not be allowed to read evaluation artifacts or the verifier.

The organizer generates the testbench and its unpredictable completion token.
Acceptance requires successful subprocess termination, the single matching token,
all 75 exact stage records across 43 cases, nonzero measured durations, assertions, and
unchanged source hashes. A simulator's exit code alone does not constitute a
pass. The same harness can run against a freshly generated gate netlist and
trusted functional cell models. Zero-delay gate simulation proves digital
function only; post-route STA is a separate mandatory physical gate.

Each run contains 32 programmable cases, each with a separately sampled private
first branch and the opposite branch for its second live-loaded program. A
candidate that ignores the branch input and independently guesses each case's
stage order passes these checks with probability 2^-32 per run; separate RTL and
gate runs use fresh private seeds. This bound describes that specific guessing
strategy, not the escape probability of every possible hardware bug. RTL
simulation has a 90-second wall-time bound; gate simulation has 1200 seconds.

The trusted observers are organizer code. Tempo's separate candidate adapter
encodes its host interface and assembles its Apache-2.0 firmware; neither its
internal register semantics nor instruction encoding are verifier requirements.
