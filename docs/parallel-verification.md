# Parallel physical verification

The first implementation overlaps the unchanged LibreLane Classic flow with
all official prechecks, geometry and delivered-GDS LVS after the fresh layout
artifacts are complete. Candidate RTL, flow settings, tool pins, official decks,
functional workloads, expected answers and scoring are unchanged.

The earlier accepted Mac run took 118.8 minutes. Its Magic DRC took 32.6 minutes
and its official precheck took 40.3 minutes, previously sequential. Overlapping
these independent checks targets that scheduling delay. It does not make an
individual single-threaded check faster. The new accepted Mac run completed in
**100m47s**, compared with **118m45s** historically: 17m58s less time (15.1%).
This is not a controlled hardware comparison; background host load differed.

## Correctness controls

- Release artifacts only after the pinned `Magic.WriteLEF` step has validated its
  output state and written its completion marker. Merely finding GDS files is not
  enough.
- Copy all five artifacts to a fresh directory, check source/copy hashes, and
  mount copies read-only. Reject escaped paths, symlinks, absent/empty inputs,
  ambiguous release stages, and changes during copying.
- Give the flow and check branch separate writable scratch. Keep the original
  Classic flow, all nine official prechecks, GDS LVS and gate tests mandatory.
- Join both branches. Require complete final flow reports, all original metric
  checks, and equality between every sealed artifact and the final delivered file.
- Use one absolute physical deadline, an aggregate output cap and owned process
  records. Cancel the sibling on failure, drain threads and subprocesses, remove
  only registered containers, and confirm container cleanup before returning pass.
- Keep gate-level compilation and simulation after the join. Their compiler
  subprocess setup is not invoked from concurrent Python threads.

A serial diagnostic schedule runs these same checks and artifact comparisons:

```sh
./verify candidates/tempo --mode full --physical-schedule parallel
./verify candidates/tempo --mode full --physical-schedule serial
```

Both require fresh output directories. Parallel mode allows two physical
containers, each retaining the existing 4 CPU / 16 GiB / 2,048 process limits.
Provision for 32 GiB plus controller/OS/service overhead; 64 GiB is recommended
when hosting this alongside durable job services. Serial mode has one active
physical container. Neither schedule accepts resource-exhausted or incomplete
runs.

## Validation

Scheduler controls exercise actual subprocess overlap, per-branch return-code
ownership, failures on either branch, shared timeout, log/output caps, absent
release, escaped/symlink inputs, copy races, artifact mutation and cleanup failure.
A real Docker timeout control launches both branches and verifies neither owned
container remains. Existing Docker compiler isolation, timeout and synthesis
negative controls also pass.

Historical baseline evidence is tested against its recorded harness identity in
unit tests; production admission still requires the currently deployed harness
hashes exactly. Historical reports are not relabeled as newly accepted evidence.

The fresh complete Tempo run passed all stages with `accepted: true`, unchanged
score **1.9403761225075868** and area **515,364 µm²**. All physical metrics and
candidate bytes match the prior baseline. Netlist, ODB, DEF and LEF are byte
identical; GDS is identical except for its 50 timestamp records. Acceptance
compared raw sealed/final bytes; timestamp normalization is reporting only.

The flow and precheck overlapped for **52m51s**. Actual running containers were
audited for their separate scratch and read-only sealed mounts, each with the
original 4 CPU / 16 GiB limits. The new Magic DRC and official precheck took
52.3 and 56.9 minutes, versus 32.6 and 40.3 historically. The Mac had substantial
background indexing/logging activity. These observations show real overlap,
but do not isolate the cause of each stage's changed duration. A dedicated GCP
Granite Rapids VM is running a sequential pair of parallel/serial evaluations.

Validation ran 83 regression tests (72 passed, 11 optional tests skipped), then
14 boundary tests and 12 scheduler tests with Docker integration enabled; all
of those passed. See the [test log](../reports/parallel-verifier-tests.log),
[measurement and comparison](../reports/parallel-validation.json), and
[complete accepted result](../reports/parallel-full.json).

Finite tests and pinned digital signoff checks do not prove all possible future
protocols or guarantee competition judging, tapeout or silicon behavior.

## Further opportunities

The pinned official precheck calls the main CMOS5L deck in deep mode. The deck's
thread-count message explicitly describes tiled mode; setting a larger thread
count alone is not demonstrated to speed up the measured connectivity bottleneck.
Changing execution mode, cutting geometry into tiles, or replacing the official
wrapper needs separate equivalence work, especially for cross-boundary
connectivity. This change does none of those things.

The PDK's separate `run_drc.py` has a table-level parallel runner, but the official
Tiny Tapeout precheck does not use that wrapper. Substituting it would change the
verification path and can duplicate expensive connectivity setup. It is not a
proven drop-in speedup for the mandated official checks.

The earlier compiler-request experiment reduced 43 isolated invocations from
17.5 to 5.2 seconds with four spawned workers. That is the next useful optimization
for fast agent iteration; production integration still needs process ownership,
ordered results, aggregate bounds, cancellation and failure tests. It is separate
from the physical scheduler implemented here.
