# Compliance boundaries and publication check

Rule source: [Jane Street's competition announcement](https://blog.janestreet.com/protocol-emulator-asic-competition/), read September 28, 2026. The table separates the announcement's requirements from this benchmark's measurement choices. A result marked `accepted: true` means technical acceptance by this verifier; it is not Jane Street acceptance or proof that an official entry was submitted.

| Announcement clause | Objective check and evidence | Remaining boundary |
|---|---|---|
| Open-source, general-purpose protocol emulator | Source/license validation; source snapshot includes RTL, loading adapter, assembler and firmware. UART/SPI/I2C pin tests plus input-dependent programmable pulse workloads run on the same hardware. | Finite tests establish the exercised capabilities, not support for every future program. |
| Reprogrammability after fabrication, rather than three fixed peripherals | Programs are loaded through physical host pins. The same elaborated RTL and separately the freshly generated gate netlist run multiple programs; 32 programmable cases each run two stages that replace firmware without rebuilding hardware or asserting chip reset between them. | No test suite proves unrestricted architectural generality. RTL/architecture review complements these behavioral checks. |
| Begin with UART, SPI and I2C | Independent external peers inspect transmitted values and compare received data with private peer inputs. Timing, I2C open-drain behavior, ACK/NACK and clock stretching are checked. | Exact modes, payload sizes and rates are this benchmark's contract, not exhaustive standards certification. |
| IHP 130 nm CMOS5L through Tiny Tapeout; use its CMOS5L template and `info.yaml` tile setting | Full mode generates the trusted Tiny Tapeout configuration using pinned CMOS5L template/support/PDK revisions. The generated `info.yaml` specifies `6x4`. | Process/template updates require an organizer-reviewed contract update. |
| Current maximum: 6×4 tiles | Fixed die and pin geometry; actual generated DEF/GDS dimensions; official Tiny Tapeout precheck. Physical results must be rebuilt from source. | The possible future 8×4 option is not enabled by this contract. |
| Run synthesis, full place/route and timing checks | Full mode requires synthesis, completed routing, finite area, required DRC/LVS/antenna/electrical metrics, constrained post-route timing, official precheck, final-GDS LVS, and independent functional gate simulation. | Digital models and process design rules do not certify manufactured silicon or arbitrary analog loading. |
| Make the submission open source | The local gate validates the complete supported license and NOTICE. The optional checker below verifies that every accepted source file is publicly retrievable at one immutable GitHub revision with exactly matching bytes. | The organizer must keep the repository public. Checking a license file does not establish authorship or ownership of contributed code. |
| Submit by January 18, 2027 | The publication report records whether its UTC calendar date is before, on or after that date. | The blog supplies no timezone or precise cutoff. Actual submission and its timestamp require the official submission service/receipt; the local verifier cannot establish them. |

The announcement's USB/Ethernet stretch goals, other suggested protocols, FPGA testing, team recommendation, language suggestions, and examples of verification methods are optional. Novelty, interesting functionality and prize selection are organizer judgments. Foundry schedules and receipt of chips/dev boards are external future events. None is represented as a passing automated requirement.

## Benchmark-specific choices

The contract fixes a 50 MHz external clock, UART 8N1, SPI mode 0 controller behavior, and single-controller I2C profiles. It initially ranks accepted designs by inverse routed standard-cell area. It accepts the supported Apache-2.0 source policy and synthesizable digital RTL; custom hard macros and unsupported constructs fail closed. These are stated evaluation limits, not additional rules claimed to come from Jane Street. Candidate architecture, instruction set, firmware and loading protocol remain replaceable through the public adapter contract.

Fast mode supplies iteration feedback only: `accepted` remains false and `score` remains null. Full mode rebuilds the layout and checks the resulting netlist before a technical score is admitted. The source snapshot, generated workloads, private replay inputs, logs, netlist/GDS hashes and verifier hashes make each evaluation reviewable. Required checks, scoring, tools and result files belong to the organizer. An agent with permission to modify the judge or its evidence can fabricate a result; running the JSON checker on participant-supplied evidence does not solve that trust problem.

## Check a public source revision

First obtain an accepted full evaluation from the trusted runner. Publish a complete source repository separately through the intended authorized workflow. Including this verifier, its setup scripts and dependency locks alongside the candidate makes the measured design reproducible. The command below only reads the public repository; it never publishes, sends messages, signs up or submits a competition entry.

```sh
python3 scripts/check_publication.py \
  --result .runs/accepted-full-run/result.json \
  --repository https://github.com/OWNER/REPOSITORY \
  --revision EXACT_40_CHARACTER_COMMIT_SHA \
  --subdirectory candidates/tempo
```

Use `--subdirectory .` when the candidate package is at repository root. The accepted result must retain its adjacent `candidate/` snapshot. `--out PATH` optionally selects the publication report; otherwise it is written as `publication-check.json` next to the technical result.

The checker requires a completed full pass, every mandatory stage, a finite positive technical score, unchanged source/license files, and the exact current trusted verifier hashes. It fetches an HTTPS GitHub archive at the exact commit with no credentials, cookies or environment proxy configuration, then compares every recorded source hash. The archive is streamed without extraction or execution. Symlinks, hard links, special files, traversal paths, duplicate names, wrong revision roots, missing files, changed bytes, malformed/truncated archives, and excessive archive sizes fail closed. Limits are 64 MiB compressed, 256 MiB decompressed and 10,000 archive members; repositories outside these limits should place the complete challenge source in a smaller repository.

A successful report establishes `technical_acceptance: true` and `public_source_matches_accepted_snapshot: true`. It always retains `official_submission_verified: false` and `organizer_acceptance_or_prize_verified: false`. Deadline status is a calendar observation, not proof of a timely submission. Before entering, review the live announcement and use the organizer's final submission process when available.

Tests for this checker use local archive fixtures and mocked downloads:

```sh
python3 -m unittest discover -s tests -p test_publication.py -v
```
