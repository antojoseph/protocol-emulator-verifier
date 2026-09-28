# Protocol emulator work

When acting as a solver, edit only `candidates/tempo/`. You may replace its RTL,
instruction set, firmware, compiler, and host protocol while satisfying the
external adapter contract. Preserve source licensing and attribution.

Run `./verify candidates/tempo --mode fast` for iteration. The provisional score
is a generic synthesis size estimate, not a physically accepted result.
Run `./verify candidates/tempo --mode full` before treating a change as a new
accepted baseline. Only `accepted: true` and a finite `score` from a full run
qualify. A blocked, failed, skipped, or incomplete stage is never acceptance.

Do not change the verifier, policy, setup, scoring, timing constraints, process,
test workloads, or expected answers to make a candidate pass. Do not read the
private runtime test artifacts during a run or move host protocol execution
outside the chip. Use the failure reports afterward to improve the candidate.

When explicitly maintaining the verifier (including the initial implementation
task), harness edits and negative-control tests are authorized. Keep benchmark
scope separate from Jane Street's published requirements; do not claim that
finite digital tests prove all future protocols or guarantee a prize/tapeout.
