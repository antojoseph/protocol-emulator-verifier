# Default verifier promotion: three physical branches

This records the earlier physical-scheduler promotion. The subsequent
[combined verifier promotion](combined-verifier.md) also enables four compiler
workers after a fresh full combined acceptance.

At this promotion, `./verify candidates/tempo --mode full` selected the fully validated
`parallel-lvs` configuration. This also applies to `benchmark.sh` and the service
worker, which invoke `verify`. The `verifier/` bytes are identical to the accepted
source commit `8c3a68b27df28a624dba997aa3ddd9beba681853`; the entrypoint supplies
the already-tested option. Direct Python library defaults remain unchanged.
Explicit `--physical-schedule parallel` or `serial` overrides the entrypoint.
No candidate or compiler-concurrency change was combined into this historical acceptance.

| Same C4, original Tempo, seed 20260928 | Two branches | Three branches |
| --- | ---: | ---: |
| Full wall time | 4448.535 s | 4249.488 s |
| Physical stage | 4002.889 s | 3816.433 s |
| Main physical flow | 3980.079 s | 3813.952 s |
| Official precheck | 2078.167 s | 2063.155 s |
| Final-GDS LVS | 232.571 s | 239.733 s |
| Accepted score | 1.9403761225075868 | 1.9403761225075868 |

The measured reduction is **199.047 seconds (4.47%) in one sequential pair**.
This is not an estimate of a repeatable speedup: most of the difference is in the
main flow, while only 19.369 seconds of the old LVS extended past that flow.
Three-way LVS was actually slightly slower in isolation. The Mac correctness
run was contended and is not used as performance evidence. Serial was deliberately
cancelled and remains incomplete; this is not a completed three-schedule study.

All mandatory checks passed, including physical timing/electrical checks, all
nine official prechecks, final-GDS transistor LVS and all 75 gate-level stages.
The original candidate, tool pins, fixed physical config, score and physical
metrics match. The new harness matches its prior full local acceptance exactly.
Netlist, ODB, DEF and LEF raw hashes match across cloud runs. Raw GDS hashes differ;
cloud timestamp-only equivalence has not been independently established. Each
run independently required all five raw sealed hashes to equal final artifacts.
Normalization is never used to accept a submission.

The result requires successful owned-container cleanup. An independent container
inventory was not collected after this cloud run because the runner had already
shut the VM down. GCP confirmed instance `1821620109997177281` was `TERMINATED`.
Result metadata is preserved in the isolated evidence bucket, and full artifacts
remain on its retained boot disk. No compute was restarted for comparison.

The default can use three physical containers, each capped at 4 CPU, 16 GiB RAM
and 2,048 PIDs. Budget 48 GiB plus controller/OS overhead; 64 GiB is recommended.
Previous two-branch release: `15c19ffa692c5cde362d10817aa6e4bac9a41b99`.
Keep that revision for exact rollback. Lower-resource hosts can select two or
one branch explicitly without dropping any acceptance check.

Evidence: [complete accepted result](../reports/c4-parallel-lvs-full.json),
[comparison and identity audit](../reports/c4-schedule-comparison.json),
[prior local acceptance](../reports/parallel-lvs-validation.json).
Entrypoint tests exercise the real argument parser for the default and both
explicit overrides. Scheduler and negative-control tests remain mandatory.

Promotion regression: 97 passed, 12 optional skips. One loopback test initially
failed because the sandbox prohibited binding; its permission-enabled retry
passed. Targeted default/scheduler tests also passed (20 passed, 2 Docker skips).
These counts overlap; frozen-source Docker negative controls remain recorded in
the earlier validation evidence.
