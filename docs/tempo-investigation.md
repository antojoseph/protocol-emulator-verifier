# Tempo investigation and verifier proposal

> Historical investigation notes, recorded before the local toolchain and independent verifier were built. Tool availability and acceptance statements below describe that initial investigation. See the [current runnable verifier](../README.md) for the implemented contract and commands.

Investigated September 28, 2026. Scope: establish whether Tempo is a useful starting point for a Yukon challenge in which agents can replace the architecture while preserving externally verified capabilities and Jane Street's published requirements.

## Finding

Tempo is a promising baseline candidate and a useful source of tests and flow integration. It is not yet independently accepted as our baseline. Its existing test suite is a design regression suite, not an architecture-independent competition verifier. Reuse its external protocol peers and physical-flow setup; build the trusted challenge contract around them.

Source revision: [`satyaammu93/jane-street-asic-2026@65e0a742c1f91ac730c137afea5e154287267d67`](https://github.com/satyaammu93/jane-street-asic-2026/tree/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator). The local investigation checkout is `/private/tmp/janes-tempo-investigation`; it is temporary, not a vendored baseline. Running the evidence audit changes its generated report, but no implementation source was changed.

## What was independently checked

| Check | Result | Interpretation |
|---|---|---|
| Python software suite | All 6 tests passed | Assembler, committed firmware, host encoding, and selected interpreter timing/reset cases reproduce. |
| Model-vector generator | Generated 24,000 vectors, seed 20260919; byte-for-byte match to committed vectors | The saved oracle vectors reproduce from current Python sources. This does not reproduce RTL/model equivalence. |
| Existing evidence audit | Failed: 50 files matched; 3 generated artifacts missing | Saved source/transcript evidence is internally useful, but the checkout does not contain the referenced final netlist/GDS. |
| RTL simulation | Not rerun | Icarus Verilog is not installed locally. |
| Formal proof, physical flow, gate simulation | Not rerun | The Docker runtime probe did not return usable server information; generated physical artifacts and PDK are absent. |

Commands, exit statuses, and tool limitations are saved in `reports/tempo-investigation.json`; raw output is in the adjacent log files. The missing files are the fill-insertion netlist, final GDS, and final netlist under `runs/closure/`. This is missing local evidence, not proof that the design fails physical checks.

## Implementation and scope

The [RTL wrapper](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/src/tt_um_protocol_emulator.v) instantiates two instruction processors, each with 64 writable 24-bit instructions, four-byte transmit/receive queues, and configurable timing. Both share eight bidirectional protocol pins. Firmware is loaded through a nibble request/acknowledgment host interface. Instruction storage is implemented as RTL arrays, not an instantiated SRAM macro.

The implementation has actual program fetch and general pin, branch, wait, data-transfer, and arithmetic operations. The supplied protocols are instruction programs. That is positive evidence for post-fabrication programmability; it is not a proof of support for every possible protocol.

The [firmware documentation](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/firmware/README.md) limits the demonstrated scope to UART 8N1 transmit/receive, SPI mode-0 controller, single-controller I2C reads/writes, and an input-event/capture/output example. I2C repeated START and multi-controller arbitration are not implemented in the supplied examples. USB, Ethernet, CAN, and other stretch protocols are not demonstrated.

The project includes Apache-2.0 `LICENSE` and `NOTICE` files. Preserve those and upstream attribution if importing it. Its PDK, tools, and cell models are separate upstream dependencies with their own notices. No source has been vendored or published by this investigation.

## Existing verification worth reusing

### External protocol tests

[`test/protocols_tb.sv`](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/test/protocols_tb.sv) operates through top-level pins. It includes resolved bidirectional wires, pull-ups, independent UART/SPI/I2C peers, I2C drive-high checks, reset checks, watchdogs, and data comparisons. It tests actual firmware loading rather than pre-populating internal instruction memory.

Useful scenarios include UART back-to-back frames and asynchronous phase/baud, framing errors and FIFO pressure; SPI full-duplex traffic; I2C ACK/NACK, stretching, read turnaround, termination, and stuck-clock timeout; and a cross-engine event response.

However, it hardcodes Tempo's host commands, registers, word lengths, firmware images, queue depth, error representation, and several exact cycle counts. Its data examples are fixed. The reported 4,574 checks include repeated host-acknowledgment assertions; they are not 4,574 independent randomized protocol scenarios. Treat that number as an assertion count, not a coverage metric.

### Internal regression and formal checks

The core/model comparison examines architectural registers and exact instruction behavior. The [31 formal assertions](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/formal/core_properties.v) concern Tempo-specific reset, pause, fault, transaction-retirement, and output state. They are useful while optimizing Tempo, but must not become mandatory semantics for an unrelated architecture.

### Physical-flow integration

[`flow-lock.json`](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/flow-lock.json) identifies the template, support tools, CMOS5L PDK, and container by revision/digest. The scripts cover synthesis, place and route, official precheck, final-GDS LVS, and evidence hashes.

Upstream's [physical report](https://github.com/satyaammu93/jane-street-asic-2026/blob/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator/docs/physical_flow.md) records a 50 MHz implementation, 515,364 square micrometres of routed standard-cell area, 31,473 standard cells, and passing checks at three supplied PVT corners with nominal interconnect RC. It also records 270 fanout advisories. These are upstream measurements, not measurements reproduced here. The saved closure used a routing checkpoint; baseline admission should include a clean end-to-end build.

## Gaps to close before agents hill climb

1. **Replace implementation assumptions with an external contract.** Keep required bus behavior and timing tolerances fixed; allow ISA, register organization, number of engines, memory organization, and firmware to change. A Tempo adapter implements that contract for the initial candidate.
2. **Verify fresh outputs.** `scripts/check_evidence.py` checks hashes and recorded completion; it does not execute proofs or fully judge physical metrics. A candidate cannot supply its own passing reports. The trusted runner must rebuild, measure, and interpret results itself.
3. **Require completed tests.** The current test runner primarily trusts subprocess exit status. A successful simulator exit alone does not establish that every intended check ran. The challenge runner needs trusted completion accounting, watchdogs, and checks for early termination. This gap was identified by source inspection, not a reproduced mutation experiment.
4. **Strengthen external coverage.** Vary payloads, addresses, lengths, phase offsets, timing, stretching, reset positions, and error cases. Verify sustained transfers with explicit host bandwidth limits, not just short preloaded examples. Add independent timing assertions; a functional bus model alone is not a compliance oracle.
5. **Separate functional gates from timing.** Tempo intentionally strips timing constructs from PDK models for functional gate simulation. That does not validate setup/hold or SDF timing. Require independent post-route static timing analysis, with fixed corners, loads, uncertainties, and reviewed exceptions.
6. **Check actual geometry and all required physical results.** Freeze the CMOS5L process, 6x4 footprint, pin template, and required rule decks. Check the generated layout and reports, rather than just the declared `tiles` value or a successful build command. Explicitly include DRC, LVS, precheck, routing, and timing gates. Do not silently inherit waivers.
7. **Test the verifier itself.** Confirm it rejects wrong received data, ignored stretching, push-pull I2C highs, stuck outputs, oversized layouts, stale reports, missing timing paths, and early simulation exit. Confirm a different but correct architecture can pass. These negative controls are a launch prerequisite.

## Proposed architecture-independent contract

The following are proposed Yukon evaluation choices, not claims that Jane Street's blog specifies these exact requirements.

### Trusted side

- Versioned protocol requirements, legal timing ranges, external wiring/loads, workload generators, reference observers, and scoring.
- Tiny Tapeout interface and physical envelope; pinned build tools and PDK; timing constraints and physical acceptance checks.
- A simulator/runner controlled by the organizer, independently collecting pin observations, completion evidence, and measurements.

### Candidate side

- Synthesizable hardware and permitted generators, programmable firmware, compiler/assembler, and a constrained loading/data-transfer adapter.
- Architecture and ISA can change completely. An adapter can translate generic test requests into the candidate's program/host format; it cannot decide whether a test passed.
- Any tuning options affecting placement/routing must be explicitly allowed and distinguished from immutable process/acceptance settings.

### Critical boundaries

- Build the hardware once per candidate and retain its netlist/GDS identity across all protocol and programmability tests. Reloading programs is allowed; resynthesizing a different chip per test is not.
- Run candidate build/compiler tools in isolation from the verifier, scores, credentials, and private test inputs. Reject unsupported or simulation-only HDL behavior; check synthesized hardware using a trusted harness and supported cell models.
- The host adapter may load programs/configuration and transfer application data under declared limits. It must not drive protocol outputs itself, access internal simulator state, or perform the timing-critical work in the host. Introduce input-dependent tests after loading and constrain host interaction during those tests.
- For programmable behavior, define a bounded behavioral workload language for pin reads/writes, waits, counted repetition, and conditional response. Candidates compile those workloads into their own ISA. This is an external capability contract, not a mandatory instruction encoding or microarchitecture.
- New seeds and workload combinations reduce overfitting. Finite testing is evidence of the specified capabilities, not a universal proof of programmability or absence of hardware bugs.

## Agent iteration and promotion

Use one authoritative acceptance rule with two execution modes:

1. **Fast local feedback:** fresh compilation, randomized external RTL tests, generic programmable workloads, and optional quick synthesis estimates. Report individual failures and provisional metrics.
2. **Authoritative acceptance:** clean pinned physical build, mandatory physical checks, post-route timing, gate-level functional tests, and final score produced by the trusted runner. Only a candidate passing this stage may become the promoted baseline.

A higher fast-loop score cannot override a failing authoritative gate. Keep local and authoritative result labels distinct. Store source/configuration hashes, tool versions, seeds, logs, and artifacts with every accepted score.

Choose the continuous objective after reproducing the baseline. A possible objective is normalized useful protocol throughput under fixed area and timing requirements, with per-workload capability floors. Measure useful completed transactions and host costs; obey protocol timing limits. Merely increasing a requested clock or reporting faster edge rates must not improve the score. If protocol limits saturate throughput, area efficiency or a separately specified programmable-I/O workload may provide a better continuous objective. Freeze the scoring definition before ranked submissions begin.

## Compliance mapping

[Jane Street's published brief](https://blog.janestreet.com/protocol-emulator-asic-competition/) is the authority for competition requirements; our verifier supplies explicit tests for objective portions.

| Blog provision | Proposed treatment |
|---|---|
| IHP CMOS5L and Tiny Tapeout template | Trusted process/tool setup and checked physical interface |
| Current 6x4 allocation | Fixed floorplan and actual layout checks; no assumption of an 8x4 increase |
| Open source | Publication/license review outside numerical scoring |
| Reprogrammable protocol engine; start with UART/SPI/I2C | Same-hardware firmware loading plus external protocol and generic programmable-workload tests |
| USB/Ethernet and other protocols are stretch suggestions | Optional future workloads; do not silently make them baseline validity requirements |
| Novel functionality and verification methodology | Human assessment; no claim that our score reproduces Jane Street's judging |
| January 18, 2027 submission | Submission scheduling and final-rule/template review outside the hardware verifier |

The blog does not prescribe every UART format, SPI mode, I2C role, throughput, memory size, or host interface. State our choices as benchmark scope. If the official rules or process change, version the contract and revalidate the baseline before comparing scores across versions.

## Recommended next milestone

Reproduce Tempo's full existing tests and a clean physical build on an available runner, then implement the external candidate adapter and independent protocol suite. Admit Tempo only after it passes that new verifier and the negative controls reject broken candidates. Package the stable runner and its score output for Yukon afterward.

This investigation created documentation and reproduced lightweight checks. It did not publish a challenge, accept a baseline, or complete the production verifier.
