# Accepted Tempo baseline

The complete frozen verifier passed on September 28, 2026. The command exited
with code 0, `status: "pass"`, `accepted: true`, and score
**1.9403761225075868**. This is a completed end-to-end evaluation, including its
own fresh physical build and subsequent gate-level tests.

```sh
./verify candidates/tempo --mode full \
  --out .runs/tempo-full-004 \
  --score-file reports/baseline-full.json
```

That evidence directory already exists. For a new evaluation, omit `--out` or
choose a new directory. Full validation took **1 hour 58 minutes 45 seconds** on
this macOS arm64 host running the pinned Linux amd64 image through Docker.
Runtime on other machines will differ.

The [compact validation record](../reports/baseline-validation.json) references
the [complete result](../reports/baseline-full.json). The original result,
candidate snapshot, generated layout/netlist, private replay inputs and detailed
logs are retained locally under `.runs/tempo-full-004/`. The copied result in
`reports/` is identical to that original result. The publication checker must
use the original result with its adjacent candidate snapshot.

## Measured results

| Gate | Result |
|---|---|
| Source and license | 29 candidate files validated; current candidate matches the accepted snapshot |
| Trusted verifier | All 18 recorded verifier files unchanged throughout the run and at final audit |
| Generic synthesis and Tiny Tapeout ports | Pass; 20,217 generic cells, used only for provisional feedback |
| RTL functional tests | 43 workloads, 75 stages, 48,803 assertions passed |
| Final gate functional tests | 43 workloads, 75 stages, 48,754 assertions passed with independently generated private inputs |
| Routed standard-cell area | **515,364 µm²** |
| Process and geometry | CMOS5L, 6×4; actual DEF/GDS dimensions 1,289.28 × 710.64 µm |
| External clock | 50 MHz |
| Physical flow | Completed cleanly; required routing, antenna, electrical, Magic DRC and LVS gates passed |
| Official Tiny Tapeout precheck | All nine checks passed; no failures, errors or skipped checks |
| Final-GDS transistor-level LVS | Passed against the independently exported reference netlist |

Assertion counts depend on the random peer inputs. The 75-stage functional
contract includes 32 programmable cases, each loading two programs without
reset between those two stages, plus UART, SPI and I2C cases. Gate simulation
uses functional cell models; timing is established separately by constrained
post-route static timing analysis.

| Timing corner | Worst setup slack | Worst hold slack |
|---|---:|---:|
| Fast, 1.32 V, −40 °C | +8.986392 ns | +0.048797 ns |
| Typical, 1.20 V, 25 °C | +7.444562 ns | +0.204760 ns |
| Slow, 1.08 V, 125 °C | +2.075781 ns | +0.486801 ns |

The ranked score is `1,000,000 / routed_standard_cell_area_um2`; larger is
better. All mandatory gates must pass before that score is accepted.

## Verifier validation

All **52 unit and real-container integration tests passed**, including source
validation, isolation, cleanup, timing/physical result rejection, provenance and
public-source archive checks. The [test log](../reports/toolchain-strengthened-tests.log)
records the run.

Two additional fresh-seed fast baselines passed. Eight intentionally broken
candidates were each tested with two seeds: **all 16 negative controls were
rejected at the intended functional assertion**, after synthesis and compilation
succeeded. Faults covered UART/SPI data, I2C high drive, programmable output and
wait behavior, host capture, premature pulses and guessing runtime branch
inputs. See the [negative-control results](../reports/toolchain-strengthened-mutation-results.json).

The [Yukon manifest audit](../reports/yukon-manifest-audit.json) also passed
against the installed CLI's actual schema and score parser. This validates the
execution contract; a hosted challenge and runner have not been deployed.

## Scope

The accepted result establishes the documented technical benchmark gates.
Finite tests do not prove support for every future protocol. Public source
availability can be checked separately with `scripts/check_publication.py`;
publication, actual official submission and organizer judging remain separate
steps. The [compliance map](compliance.md) distinguishes the blog's requirements
from benchmark choices and external facts.
