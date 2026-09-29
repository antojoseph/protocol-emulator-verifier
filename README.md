# Programmable protocol emulator verifier

A local agent hill-climbing harness for Jane Street's [protocol-emulator ASIC
competition](https://blog.janestreet.com/protocol-emulator-asic-competition/).
Candidates can replace the implementation and ISA. The judge checks external
protocol behavior, program loading and input-dependent operation, and the fixed
CMOS5L physical envelope. Tempo is the included starting candidate.

**Validated baseline:** Tempo passed the complete full run, including all nine
official prechecks and 75 gate-level functional stages. Routed standard-cell
area is **515,364 µm²**, with accepted score **1.9403761225075868**.
See the [validation results and evidence](docs/validation.md).

## Run

Prerequisites: Python 3.9+, Git, and a running Docker engine. The physical tools
and precheck dependencies run in the pinned container. Setup downloads are
installed under `.tools/`; it does not install packages globally.

```sh
./setup.sh                         # pinned container for compiler and RTL tools
./verify candidates/tempo --mode fast

./setup.sh --physical              # pinned CMOS5L PDK, tools, and precheck packages
./verify candidates/tempo --mode full

# One full evaluation with a machine-readable artifact at a known location:
./verify candidates/tempo --mode full --score-file score.json
```

Every run prints its evidence directory. It contains `result.json`, a complete
candidate snapshot, source/harness hashes, generated workloads, and tool logs.
Use `--out PATH` for a new named evidence directory, `--seed N` to vary public
workloads, and `--physical-timeout SECONDS` for slow machines. Existing evidence
directories are never overwritten. Private peer inputs are freshly randomized
after candidate compilation; public seed alone does not predict them.

Exit codes: `0` = requested mode passed, `1` = failure, `2` = missing required
infrastructure. **A fast pass always has `accepted: false` and `score: null`.**
Only a full pass has `accepted: true` and a finite ranked benchmark score. Consumers must
check those fields, not just the exit code. Full acceptance covers technical
benchmark gates; official publication/submission steps remain explicit.

## Hill climbing

Give agents this repository and `AGENTS.md`. Their editable area is
`candidates/tempo/`; everything under `verifier/` is trusted organizer code.
The manifest specifies a top-level module, RTL files, a compiler adapter, license,
and attribution. The adapter runs in a read-only, network-disabled, resource-
limited container and returns bounded JSON host-pin actions. It never supplies
test verdicts, expected receive bytes, scores, or physical reports.

The initial objective is **minimize routed standard-cell area** while preserving
the required behavior and passing timing at a 50 MHz external clock:

`score = 1,000,000 / routed_standard_cell_area_um2` (higher is better).

Fast mode reports a separately labeled generic-cell estimate. It may disagree
with physical area and is only directional feedback. Gates, memory choices,
firmware, datapaths, and host encoding can change. The same compiled hardware
must run every workload; only loaded programs/configuration may change.

The fixed 50 MHz clock and particular UART/SPI/I2C test profiles are benchmark
choices, not extra rules attributed to Jane Street. The blog does not specify a
unique scalar objective or exhaustive protocol modes. Contract v1 currently
accepts synthesizable digital Verilog/SystemVerilog, no custom hard macros or
simulation-only constructs, and the included Apache-2.0 license policy.
Unsupported features fail closed. These choices can be expanded in a new
reviewed contract without fixing the architecture to Tempo.

## What passing establishes

- Complete source package with the recognized open-source license and NOTICE.
- UART 8N1 TX/RX, SPI mode-0 controller, and single-controller I2C read/write
  behavior under the specified randomized tests, including error/stretch cases.
- Program reloading and runtime input-dependent pin/timing workloads on one
  hardware implementation.
- In full mode: a fresh 6x4 CMOS5L build, constrained timing checks, required
  physical metrics, official Tiny Tapeout precheck, final-GDS LVS, and functional
  simulation of the generated netlist.

These are measured digital/physical-flow checks. Analog bus loading, arbitrary
future programs, silicon behavior, legal authorship, novelty, and competition
judging cannot be certified by this test suite. `official_submission_ready`
remains false: publish the complete source, review the current official rules,
and submit by January 18, 2027 through Jane Street's process. The verifier records
whether that deadline date has passed; the blog does not specify its timezone.
USB/Ethernet and the other suggested protocols are stretch work, not mandatory
baseline gates. An FPGA demonstration is optional in the blog.

See [physical gates](docs/physical-verifier.md),
[Tempo investigation](docs/tempo-investigation.md),
[compliance and public-source verification](docs/compliance.md), and the trusted contract
under `verifier/protocols/`. `benchmark.json` and `benchmark.sh` provide a Yukon
schema-v1 execution contract. Platform onboarding/runner registration is a
separate deployment step; no challenge has been published. See the validated
[Yukon integration requirements](docs/yukon-integration.md).

An [isolated asynchronous GCP service](docs/isolated-gcp-service.md) can run agent
submissions through a private operator API, with durable PostgreSQL job status,
separate fast/full workers, restart recovery, and checksummed GCS evidence.
See [deployment validation](reports/service-validation.json) for its current
readiness. It uses this same verifier; Yukon production onboarding remains separate.

## Test the verifier

```sh
RUN_VERIFIER_INTEGRATION=1 python3 -m unittest discover -s tests -v
python3 scripts/test_mutations.py
```

The mutation tests must distinguish real verifier rejection from infrastructure
failure. Keep the verifier outside the candidate archive on ranked runners and
run only trusted workflows; local filesystem access is not a security boundary
against an agent allowed to edit the judge itself.

For optional native debugging/formal tools, `python3 scripts/setup_simulator.py`
installs a checksum-pinned OSS CAD Suite locally. Ranked evaluation uses the
container tools regardless of native installations.

## Attribution

Tempo source: [`satyaammu93/jane-street-asic-2026`, revision
`65e0a742c1f91ac730c137afea5e154287267d67`](https://github.com/satyaammu93/jane-street-asic-2026/tree/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator),
Apache-2.0. Its source license and notices are preserved in the candidate.
Adapted physical-flow support is attributed separately under
`verifier/physical_support/`. The toolchain and PDK retain their own licenses.
