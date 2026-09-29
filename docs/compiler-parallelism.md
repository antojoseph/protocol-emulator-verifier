# Validated parallel candidate compilation

The public entrypoint uses four workers by default; `--compiler-workers {1,2,4}`
selects an explicit limit. The Python library default remains one worker. It changes compiler scheduling, not the candidate, public workloads,
private-peer generation, expected answers, physical flow, tool pins or score.
A fresh full run on the original Tempo candidate passed every mandatory gate
with accepted score 1.9403761225075868. See the [full result](../reports/compiler-parallel-full.json)
and [identity audit](../reports/compiler-parallel-validation.json). This acceptance
uses the two-branch physical schedule. A subsequent [combined full validation](combined-verifier.md)
also passed with the three-branch schedule and the intended public defaults.

```sh
./verify candidates/tempo --mode fast --compiler-workers 4
```

Four spawned Python workers invoke the existing isolated adapter compiler.
Every request still gets its own fresh network-disabled, read-only candidate
container, one CPU, 512 MiB memory, 64-process limit, bounded output and existing
timeout. There is no persistent candidate interpreter or shared candidate
scratch. At most four requests are dispatched at once, with maximum compiler
container limits of four CPUs and 2 GiB memory in aggregate. Host controller
and Python worker memory are additional.

Spawned workers keep the existing `preexec_fn` resource setup out of controller
threads. The executable entrypoint now has a `__main__` guard so spawning does
not recursively launch evaluations. Results return in original workload order,
regardless of completion order. The complete batch joins and checks container
cleanup before the functional harness validates the responses and generates
fresh private peer inputs. All 43 workloads and all 75 stages remain required.

Failure stops further dispatch, cancels work that has not started and drains
active calls under their existing 25-second compiler and 10-second cleanup
bounds. It does not abruptly kill workers during normal cancellation. The
parent preassigns unique container names, removes only its own survivors after
joining, and confirms their absence, including after a worker crash. Inability
to confirm cleanup is failure. Partial responses never reach simulation or
acceptance. Each batch requires a new output directory.

## Validation

The targeted suite covers real spawned-process overlap and worker limits,
ordered results, failure stopping dispatch, draining before cleanup, crashed
workers, cleanup failure, invalid concurrency, reused evidence directories,
and prevention of private-input generation following a failed batch. Docker
tests compare parallel adapter outputs against serial outputs and exercise a
failing request alongside a timed-out request, checking owned containers are
removed. All ten targeted tests passed with integration enabled.

The broader regression ran 109 tests: 95 passed, 13 optional integrations were
skipped, and the localhost API test initially encountered the filesystem
sandbox's socket restriction. That one test then passed with permission to bind
its ephemeral loopback port. No production API or cloud resource was touched.

Four complete fast evaluations use order 1, 4, 4, 1 workers, the same original
candidate and public seed, and fresh private tests each time. Results and
timings are in [the measurement report](../reports/compiler-parallel-fast.json).
The Mac also runs two independent physical candidate evaluations. This small
sample supports local fast-path feedback measurements, not a dedicated-host
performance guarantee or a full-acceptance claim.

All four evaluations passed with unchanged 20,217 generic cells and complete
43-workload/75-stage RTL coverage. One-worker runs took 30.18 and 30.03 seconds;
four-worker runs took 15.90 and 16.30 seconds. Median elapsed time fell from
**30.11 to 16.10 seconds (46.5% less time)**. Candidate and harness hashes were
identical across the four runs; private test inputs remained fresh.

The longer full-run opportunity is overlapping gate simulation with physical
signoff after the sealed-netlist barrier. That needs separate process ownership,
cancellation, immutable model/netlist checks and a fresh full validation; this
branch does not implement it. Compiler concurrency primarily saves seconds
per iteration and cannot by itself make physical signoff fit 15–20 minutes.
