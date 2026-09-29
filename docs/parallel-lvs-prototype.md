# Opt-in independent final-GDS LVS

This branch adds the opt-in `parallel-lvs` schedule. A fresh full EDA verification
of the original Tempo candidate completed with **`accepted: true` and unchanged
score `1.9403761225075868`** on September 29, 2026. All four mandatory stages
passed. The evaluated harness is commit
`8c3a68b27df28a624dba997aa3ddd9beba681853`; the default remains `parallel`.
**A performance improvement has not been established.**

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

This opportunity depends on the machine's critical path. A subsequent accepted
run of the existing `parallel` schedule on the dedicated C4, reported by the
parent task, finished hardening at monotonic time 4,123.358 s and final-GDS LVS
at 4,142.727 s. Only **19.37 seconds** of LVS extended beyond hardening there.
That substantially smaller removable tail can be outweighed by the new branch's
contention. The five-minute estimate must not be applied to that C4 run. A
controlled comparison on the same machine is still needed.

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

## Validation evidence and performance limits

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
performance. The remaining regression modules ran separately: **51 passed and
7 skipped** because isolated PostgreSQL was not configured; service database
code is unchanged. Together, **85 applicable tests passed**. See the
[remaining regression log](../reports/parallel-lvs-remaining-regression.log).

The fresh full run started at **05:42:36.642125 UTC** and completed at
**07:04:21.068452 UTC**, taking **4,904.426327 seconds (81m44s)**. The Mac also
ran a solver's full evaluation and unrelated existing workloads, so this is
correctness validation, not a controlled timing comparison. The complete
[accepted result](../reports/parallel-lvs-full.json) and
[validation summary](../reports/parallel-lvs-validation.json) retain the evidence.

- Candidate source hashes match original Tempo from commit `9a33294` exactly;
  candidate and harness hashes remained unchanged during the run.
- All physical metrics match the accepted original, including area
  **515,364 µm²** and all three corners' setup/hold slacks.
- Netlist, ODB, DEF and LEF are byte-identical to the accepted original. GDS is
  identical after ignoring only its 50 timestamp records for historical
  comparison. Acceptance itself compared all five sealed/final artifacts using
  their exact, unmodified bytes.
- The actual three running containers passed the read-only mount, separate
  scratch, networking and resource-cap audit at **06:16:59 UTC**. Final-GDS LVS
  overlapped both hardening and official precheck for its full **312.558315 s**.
- Mandatory gate simulation passed before acceptance. An independent container
  inventory afterward confirmed all seven owned physical containers absent.

The completed command was:

```sh
JANES_PHYSICAL_TOOLS=/Users/fork/Documents/ChatGPT/janeS/.tools/physical \
  ./verify candidates/tempo --mode full --physical-schedule parallel-lvs \
  --out .runs/tempo-full-parallel-lvs-001
```

Before selecting this schedule for speed, compare it with the unchanged accepted
candidate under the existing `parallel` schedule on the same otherwise idle
machine. Retain command timings, artifact comparisons, actual container
mount/limit evidence, and fresh acceptance results. A timeout, missing report,
hash mismatch, or incomplete stage cannot qualify as acceptance. Correctness
acceptance does not establish a performance win; a slower schedule may still be
technically valid. This branch was not deployed to GCP or merged into the main
checkout during validation.

Gate simulation overlap ranks second: it could hide several more minutes but
needs spawned-process isolation and a larger acceptance/cancellation change.
Parallel compiler requests rank third for this critical path: they help fast
iteration by seconds. Neither is implemented here. Splitting or replacing the
official KLayout connectivity/deep checks requires a separate coverage argument
and is not part of this prototype.
