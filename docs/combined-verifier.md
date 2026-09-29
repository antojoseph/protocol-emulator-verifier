# Validated combined default

The normal agent commands now use four isolated compiler workers and three
independent physical branches:

```sh
./verify candidates/tempo --mode fast
./verify candidates/tempo --mode full
```

The same defaults apply to `benchmark.sh` and the service worker. The exact source
and entrypoint frozen at `21feb90` passed a new full run using these defaults,
original Tempo and public seed 20260928. All mandatory checks completed, including
all nine official prechecks, physical/electrical/timing checks, final-GDS LVS,
and all 75 gate-level stages. Accepted score remains **1.9403761225075868** at
**515,364 µm²**. No candidate optimization is included.

The run took **77m07s (4627.299 seconds)** on the Mac. This establishes combined
correctness; it is not a controlled speed comparison with C4 or older Mac runs.
The compiler-only ABBA experiment measured median fast feedback of **30.11s →
16.10s**, a 46.5% reduction, on a contended Mac. The earlier C4 physical comparison
measured **74m09s → 70m49s** in one pair, with most variation in the main flow.
Neither observation promises those exact gains on another host. Full verification
has not reached the requested 15–20 minute target.

The audit independently checked candidate/harness/entrypoint identities, fixed
configuration and tool pins, every check, service result admission and actual raw
sealed/final artifact hashes. All physical metrics match the accepted original
candidate. A read-only Docker inventory confirmed all seven owned physical
containers and compiler containers absent. Unrelated containers were untouched.
Cloud compute remains stopped; this validation did not use GCP.

The physical phase allows three containers of 4 CPU / 16 GiB each; budget 48 GiB
plus host overhead (64 GiB recommended). Compiler batches allow four containers
of 1 CPU / 512 MiB each plus Python/controller overhead. These phases run
sequentially. Explicit options remain available:

```sh
./verify candidates/tempo --mode full --physical-schedule parallel --compiler-workers 1
./verify candidates/tempo --mode full --physical-schedule serial --compiler-workers 1
```

Every schedule still requires all acceptance gates. For an exact source rollback,
use previous accepted revision `ad60484` (three physical branches, serial
compiler); the earlier two-branch release remains `15c19ff`. Diagnostic option
combinations are not separately claimed as fresh full accepted results.

Regression validation passed 106 checks after one sandbox loopback retry, with
14 optional skips. All 34 targeted tests, including actual Docker compiler and
scheduler isolation/failure cleanup plus default/override parsing, passed.
Counts overlap. No verifier or entrypoint bytes changed after the full run.

Evidence: [full result](../reports/combined-verifier-full.json),
[identity/artifact/cleanup audit](../reports/combined-verifier-validation.json),
[compiler speed measurements](../reports/compiler-parallel-fast.json),
[C4 comparison](../reports/c4-schedule-comparison.json), and
[preflight integration tests](../reports/combined-verifier-integration.log).

Both bounded solver approaches were rejected. One timed out in detailed routing;
the smaller single-engine design ultimately passed physical signoff but failed
UART transmission in gate simulation. Its cause remains unresolved. Neither
produced an accepted score or changed the baseline. Their
[terminal outcomes](../reports/bounded-solver-outcomes.json) preserve that distinction.
