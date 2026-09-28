# Authoritative CMOS5L physical acceptance

`verifier/physical.py` rebuilds candidate RTL from an empty output directory using
pinned Tiny Tapeout CMOS5L process files and LibreLane. Candidate configuration,
reports, checkpoints, executables, and timing exceptions are never imported.
Its returned `pass` is the **physical stage only**. The challenge CLI must also
pass its independent functional tests against the returned `gate_netlist` before
issuing an accepted score.

## Installation and execution

Requirements: Git, host Python 3.9 or newer, and a running Docker engine with Linux amd64 support, adequate disk space, and network
access during setup. Docker Desktop on Apple Silicon runs the pinned amd64 image
through emulation; physical runs can be substantially slower than a native Linux
runner. The verification containers have no network access.

```sh
./setup.sh --physical
```

The default cache is `.tools/physical`. To select another location, use
`--tools-root PATH` at setup and set `JANES_PHYSICAL_TOOLS=PATH` at evaluation.
The setup does not change an existing checkout at a different revision.
`--skip-image` skips downloading the image; it still requires the image for its
dependency smoke test. No host EDA tools or Python packages are installed.
Precheck runs entirely inside the pinned image using Python 3.13 and four
checksum-pinned Linux wheels. Every extracted package byte is verified against
its pinned archive before execution; modified or added cache files fail closed.

The normal entry point is the challenge CLI. The physical module can also be
called directly for diagnostics:

```python
from verifier.physical import run_physical
result = run_physical("candidates/tempo", "runs/fresh-physical-run", timeout=18000)
assert result["status"] == "pass", result["checks"]
```

Every invocation requires a new empty output directory. It records commands,
logs, source hashes, configuration hash, GDS/netlist hashes, and measured results
in `physical-result.json`. Missing tools produce `blocked`; absent reports,
nonfinite metrics, missing checks, timeouts, or violations produce failure.
Neither outcome can be scored or promoted.

## Immutable physical contract

- IHP `ihp-sg13cmos5l`, standard-cell library `sg13cmos5l_stdcell`.
- PDK commit `2bbec755dc67ca3db0261c3d6163e15735d66710`.
- Tiny Tapeout support commit `f6bf5c587fba4a4a8abd4c0a03234fccfbf6e61e`.
- LibreLane `3.1.0.dev3` container digest
  `sha256:d109140b8f17fc54f4fca998beb8124f4949404ec52e339eebd2250854a18b5a`.
- Official 6×4 pin/floorplan DEF: 1289.28 × 710.64 µm, verified against both the
  generated DEF and actual final GDS extent.
- One `clk` input, 20 ns period, 4 ns input/output budgets, 0.25 ns uncertainty,
  6 fF output load. The clock must propagate through the routed implementation.
- All three supplied nominal-interconnect PVT corners:
  `nom_fast_1p32V_m40C`, `nom_typ_1p20V_25C`, `nom_slow_1p08V_125C`.
- RTL-only standard-cell implementation. Hard macros and custom timing exceptions
  need a new reviewed verifier contract; candidate metadata cannot activate them.

The process and 6×4 limit derive from the competition. Fixed 50 MHz timing and
digital-only synthesis are this benchmark's concrete evaluation choices, not a
claim that the blog mandates that clock or excludes other possible submissions.
All implementation, instruction-set and firmware changes within this contract
remain allowed.

## Required fresh evidence

1. Fresh Yosys elaboration confirms the exact Tiny Tapeout port directions and
   widths, then the complete Classic flow starts from RTL.
2. The flow reaches the final manufacturability report with fresh synthesis and
   post-route timing stages. No inferred latches, unresolved cells, lint timing
   constructs, synthesis errors, disconnected pins, power-grid violations,
   routing DRC, antenna violations, Magic DRC, illegal overlaps, or Netgen LVS
   differences are accepted.
3. Every PVT corner has finite nonnegative setup/hold slack and zero setup/hold,
   slew, capacitance, or unannotated signal-net violations. Actual min/max path
   reports must contain timed endpoints; the detailed constraint audit must
   complete without warnings, unclocked or unconstrained endpoints. A finite
   positive standard-cell area is required.
4. Official CMOS5L precheck must complete exactly its nine checks with no skipped,
   failed or missing cases: pin-label overlap, CMOS5L KLayout DRC, zero area,
   layer/top/boundary checks, pin placement, boundary, valid layers, cell names,
   and the digital project's analog-pin check.
5. A separate transistor-level LVS extracts the delivered final GDS using the
   pinned IHP KLayout deck and compares it to CDL exported from the final routed
   ODB with pinned transistor cell masters. It requires an explicit netlist match
   and nonempty extracted device counts; a zero process status alone is rejected.
6. Input and generated artifact hashes remain unchanged through verification.
   The final netlist and generated zero-delay models (with original/generated
   hashes) return to the trusted functional evaluator for another run of the same externally observed workloads.

The official flow's maximum-fanout count is retained as an advisory; acceptance
requires the actual electrical slew/capacitance limits and timing constraints.
The baseline upstream reported fanout advisories predominantly on clock-tree
nets. This explicit treatment does not waive timing, DRC, or connectivity errors.
Nominal-RC PVT checks are exactly the supplied flow scope, not an independent
min/max-RC characterization or board/silicon qualification.

## Trust boundary and limitations

`config` arguments are organizer-only locations/job limits. They cannot change
physical limits, corners, test decks, or timing budgets. Source/config/script
mounts, the official helper tree, generated metadata and PDK are read-only inside
containers; only the helper's reports directory receives a writable child mount. Containers
receive no Docker socket, network, elevated capabilities, or credentials. Each
container is limited to 4 CPUs, 16 GiB RAM and 2,048 processes. The runner
terminates stages exceeding their deadline, 256 MiB per log or 16 GiB total
generated output. Official precheck and geometry parsing use the same container
boundary. The Python controller only handles trusted metadata and bounded reports.
The organizer controls and protects the verifier checkout, dependency cache and
output directories. Agents should only receive the candidate workspace. As with
any toolchain-based verifier, this is not a security guarantee against unknown
EDA tool or container-runtime vulnerabilities.

The official precheck catches objective rules available in its pinned deck.
The competition's novelty/quality judging, open publication, deadline handling,
submission review and eventual silicon operation are outside physical signoff.
If the official process/template or allocation changes, update and review the
pins, version the benchmark, and re-run the baseline; do not silently mix scores.

## Attribution

The Docker/PDK pins, floorplan configuration, precheck integration and final-GDS
LVS approach were adapted from
[Tempo at 65e0a742](https://github.com/satyaammu93/jane-street-asic-2026/tree/65e0a742c1f91ac730c137afea5e154287267d67/protocol_emulator),
licensed Apache-2.0. Its notices are retained under
`verifier/physical_support/LICENSE.tempo` and `NOTICE.tempo`.
The official support scripts and PDK are separately downloaded at pinned
revisions and retain their upstream licenses. `flow-lock.json` records all
upstream source URLs, revisions and the image digest.
