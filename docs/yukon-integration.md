# Yukon manifest and runner integration

The manifest was checked against the actual installed Yukon CLI bundle on September 28, 2026: CLI `v2026.08.25-2`, executed with Bun `1.3.14`. This is a local compatibility check, not an assertion that a benchmark has been imported, deployed or opened for submissions.

The audit extracted the installed bundle's unchanged Zod, manifest, path-normalization and score-parser code into a temporary module. It invoked those parsers directly, excluding the CLI entry point, account configuration, authentication, trace collection and telemetry. No CLI setup/run/submit command or external service operation was invoked. Results and the installed bundle checksum are retained in [the audit artifact](../reports/yukon-manifest-audit.json).

The original manifest failed because the installed parser requires a nonempty `category`. Adding `"category": "hardware"` made the manifest valid. The actual parsed contract is:

| Field | Verified value/meaning |
|---|---|
| `schemaVersion` | `1`: one standalone benchmark |
| `name` | `programmable-protocol-emulator` |
| `category` | `hardware`; the local parser accepts a nonempty category string |
| `direction` | `+`: higher technical score is better |
| `editablePaths` | Only `candidates/tempo` |
| `setupCommand` | Argument array invoking `./setup.sh --physical` through Bash |
| `benchmarkCommand` | Argument array invoking `./benchmark.sh` through Bash |
| `scorePath` | `score.json`, outside the editable candidate directory |
| `maxSubmissionBytes` | `8388608`, valid under the installed schema |
| `minScoreImprovementBips` | `1`, a nonnegative integer accepted by the parser |

Negative parser controls rejected a string instead of a command array, a score file inside the editable candidate directory, and making `benchmark.json` editable. The score parser accepted a finite numeric score fixture and rejected null scores, numeric strings and infinity. This is compatible with the harness: fast/failed evaluations emit null scores, and only the fixed full benchmark command can finish successfully with a technical score.

Yukon's score parser itself does not establish source correctness or interpret `accepted`; the trusted benchmark command and its nonzero failure exit provide that boundary. The local Yukon runner removes a previous score before invoking the command and reads the new score only after successful command exit. Its command executor waits for completion without defining a command-level timeout in this manifest schema.

## Runner requirements

The setup command prepares dependencies locally. It requires host Python 3.9+, Git, Bash, and a functioning Docker engine capable of running the pinned Linux amd64 LibreLane image. Linux amd64 avoids architecture emulation; the same image was also exercised through Docker Desktop on Apple Silicon. Setup needs outbound access to the pinned GitHub repositories, container registry and Python wheel download URLs. Candidate execution containers themselves have networking disabled.

The verifier explicitly limits compiler containers to 512 MiB and one CPU, fast EDA containers to 8 GiB and two CPUs, and physical containers to 16 GiB and four CPUs. The host needs sufficient RAM/disk beyond those container limits. Physical output is capped at 16 GiB per evaluation, in addition to images, dependencies and retained run evidence. A new run creates fresh evidence; retained runs require storage management by the organizer.

Local wall-time bounds are 25 seconds per compiler invocation, 180 seconds for fast synthesis, 90 seconds for HDL compilation and RTL simulation, 1,200 seconds for gate simulation, and 14,400 seconds for the physical stage by default. `--physical-timeout` can change the last bound in an organizer-controlled command. Setup downloads are not covered by one overall job timeout. The deployed runner must impose a separate overall setup/evaluation deadline that accounts for these stages and cleanup. Physical mode overlaps two independent branches by default; provision for their combined 32 GiB container memory limits plus host/service overhead, or select the serial physical schedule on smaller hosts.

`benchmark.json` does not currently declare a `runner`. The installed schema supports optional `github-actions` with a workflow filename, or `frontiercs`; neither provisions CPU, memory, disk, Docker access, or timeouts. An invented field such as `timeoutSeconds` is silently stripped by the schema-v1 parser, so it would not enforce a limit. This repository intentionally does not claim that an absent workflow or an unspecified remote runner is configured.

Importing the repository into Yukon, choosing a supported runner, granting its required Docker capability, configuring job limits and artifact retention, and establishing the trusted submission/promotion workflow remain external deployment steps. Do not expose the host Docker socket or verifier sources as writable resources to candidate programs. Keep the organizer's judge and result evidence separate from participant-editable source. No benchmark launch, publication or submission was performed by this audit.
