# Experimental independent final-GDS LVS

This branch adds the opt-in `parallel-lvs` schedule. It has passed bounded
scheduler and negative-control tests, including real Docker cleanup tests, but
**has not completed a full EDA verification run**. It is not an accepted
replacement harness. The default remains `parallel`.

The fully accepted starting point is commit
`9a3329419eb4859fc5ae230c21824c3bbd8e6f72`. Its measurement is in
[`parallel-validation.json`](../reports/parallel-validation.json): 6,047.40 seconds
total, 5,164.09 seconds hardening, 3,411.68 seconds official precheck, and 302.62
seconds final delivered-GDS LVS. Precheck continued roughly 241 seconds after
hardening ended; CDL export and final-GDS LVS then added another 303.60 seconds.

The first additional opportunity is therefore to start CDL export and final-GDS
LVS as soon as the existing sealed-artifact barrier releases, alongside official
precheck. Holding other durations fixed, this could remove about **5.1 minutes**
(5% of that full run), taking roughly 100.8 minutes to 95.7 minutes. This is a
critical-path estimate, not a measured speedup. Extra CPU and memory contention
can offset it, especially on a busy Mac or an undersized VM.

## Execution and acceptance

```text
fresh flow ── Magic.WriteLEF completes ── original remaining flow ───────┐
                    │                                               │
                    ├─ official precheck ── GDS geometry ────────────┤
                    └─ export routed CDL ── final-GDS LVS ────────────┤
                                                                    │
          join every branch → audit final reports and exact hashes ──┘
                            → mandatory gate simulation → verdict
```

All nine official prechecks, the complete original flow, delivered-GDS LVS,
geometry checks, multicorner timing audit, byte-identical sealed/final artifact
checks, source provenance, and functional gate checks remain mandatory. The
official decks, tool pins, deep-mode settings, constraints, workloads, scoring,
and candidate are unchanged.

`checks/` and `lvs_checks/` are separate writable scratch trees. Each side
container sees its own tree plus read-only sealed artifacts and the pinned PDK;
it cannot access the other side's scratch or the main flow's work directory.
The main flow sees both side trees read-only. Exported CDL is copied into the
LVS branch's read-only `trusted/` mount, with a hash checked after LVS finishes.

The same `PhysicalRunner` owns all child processes, unique container names,
the absolute physical deadline, per-stage log caps, and the aggregate generated
output cap across all three branches. Failure in any branch cancels the others.
Side futures are observed in completion order, so a report-validation failure
cannot be hidden behind another long-running check. Both scheduler pools are
fully joined before any compiler or gate simulation starts; no candidate
compiler using `preexec_fn` is launched from these threads. Cleanup must confirm
that every owned container is gone before a pass can be returned.

Maximum physical limits become **three containers × 4 CPUs / 16 GiB / 2,048
processes**, or 12 CPUs / 48 GiB / 6,144 processes in aggregate. Plan for host
overhead beyond those limits; 64 GiB is a reasonable test-host minimum. The
existing `parallel` and `serial` schedules retain two- and one-container bounds.
They still perform the same checks, with the two side groups sequential.

## Evidence and remaining validation

[`parallel-lvs-prototype-tests.log`](../reports/parallel-lvs-prototype-tests.log)
records **34 passing targeted tests**, including:

- Real three-way subprocess rendezvous, deterministic report ordering, and
  complete joining before returning.
- Failure in each of the three branches; report failures in either side branch,
  including after the main flow has exited; shared timeout and aggregate output
  cap enforcement.
- Two- and three-container Docker timeout cleanup, removing only owned names.
- Synthetic orchestration of all three schedules, checking every mandatory
  side command, independent scratch mounts, read-only inputs and resource caps.
- Sealed/final artifact mutation and generated-CDL mutation rejection, plus the
  existing negative controls for physical reports and timing evidence.

Synthetic reports are deliberately incomplete and cannot produce acceptance.
These tests demonstrate scheduling and failure controls, not EDA correctness or
performance. A new full run of this exact harness must still return
`accepted: true` and a finite score. The planned run, when the host is available:

```sh
JANES_PHYSICAL_TOOLS=/Users/fork/Documents/ChatGPT/janeS/.tools/physical \
  ./verify candidates/tempo --mode full --physical-schedule parallel-lvs \
  --out .runs/tempo-full-parallel-lvs-001
```

Compare it with the unchanged accepted candidate under the existing `parallel`
schedule on the same otherwise idle machine. Retain all command timings,
artifact comparisons, actual container mount/limit evidence, and fresh acceptance
results. A slowdown, timeout, missing report, hash mismatch, or incomplete stage
must not be treated as success. No heavy full run was started during this
prototype task, to avoid contention with the solver's active Mac run and the
controlled GCP benchmark.

Gate simulation overlap ranks second: it could hide several more minutes but
needs spawned-process isolation and a larger acceptance/cancellation change.
Parallel compiler requests rank third for this critical path: they help fast
iteration by seconds. Neither is implemented here. Splitting or replacing the
official KLayout connectivity/deep checks requires a separate coverage argument
and is not part of this prototype.
