# Why the cloud full verifier was slower

Investigated September 29, 2026. The `c3-standard-8` VM took 146.3 minutes
versus 118.8 minutes on the Mac, a 23.2% increase. The dominant checks execute
more slowly per CPU thread on this VM. The original expectation that native
x86 execution would improve full-run latency was not supported by measurement.
The deployed verifier, acceptance checks, candidate, and resource settings
were not changed for this investigation.

Raw measurements and comparison methodology are in
[cloud-performance-investigation.json](../reports/cloud-performance-investigation.json).

## Actual hardware and allocation

| | Mac | Cloud worker |
|---|---|---|
| Processor | Apple M3 Max | Intel Xeon Platinum 8481C |
| Physical cores | 16: 12 performance, 4 efficiency | 4, exposed as 8 hardware threads |
| Host memory | 128 GiB | 32 GiB |
| Docker resources | 14 virtual CPUs, approximately 94 GiB | 8 vCPUs, approximately 31 GiB visible |
| Pinned amd64 tool execution | Apple virtualization with Rosetta | Native x86-64 |
| Physical container allowance | 4 CPUs, 16 GiB | 4 CPUs, 16 GiB |

Host inspection confirmed the topology. Google also documents that a C3 vCPU
is one hardware thread, with two threads per core. Eight vCPUs do not mean
eight physical cores. See [Google's C3 documentation](https://docs.cloud.google.com/compute/docs/general-purpose-machines#c3_series).

The physical wrapper hardcodes `--cpus=4`; OpenROAD also receives four jobs.
The host worker has no additional systemd CPU quota. Increasing the machine's
vCPU count alone would leave the existing container allowance unchanged.
A Docker CPU quota is a scheduling-time allowance across logical CPUs, not
a reservation of dedicated physical cores.
[Docker resource constraints](https://docs.docker.com/engine/containers/resource_constraints/)
explain this distinction.

Rosetta was enabled in Docker's settings and appeared in the diagnostic
process's memory mappings. Native execution removes translation overhead,
but that alone does not establish which processor finishes a workload sooner.
See [Docker's Rosetta setting](https://docs.docker.com/desktop/settings-and-maintenance/settings/).

## Where the extra 27.55 minutes went

| Work | Mac | Cloud | Added time |
|---|---:|---:|---:|
| Magic design-rule check | 32.64 min | 42.79 min | 10.15 min |
| Official KLayout design-rule check | 40.32 min | 49.83 min | 9.52 min |
| Detailed routing | 24.23 min | 29.12 min | 4.90 min |
| Complete verifier | 118.75 min | 146.30 min | 27.55 min |

These three physical stages explain **89.15% of the overall slowdown**.
Magic used one thread at approximately 100% CPU on both machines. Its user CPU
time essentially equals wall time, with zero reported I/O wait. Routing
averaged approximately 3.8 CPUs on both machines, also with zero reported
I/O wait. Its eight observed OS threads were not eight continuously busy CPUs.

Official KLayout connectivity setup grew from 36m20s to 45m29s, accounting
for 549 of the check's 571 additional seconds. Both runs used hierarchical
mode with `deep:true tiled:false threads:4`. A later log mentioning 14 or 8
threads describes tiled-mode capacity; it does not show that the active
connectivity operation used that many threads.

The two design-rule checks ran sequentially and consumed 63.3% of the cloud
verifier's total time. More unused cores or RAM cannot automatically accelerate
their serial work. The evidence supports CPU execution throughput as the
dominant limitation. It does not isolate clock, cache/memory behavior,
microarchitecture, or an exact contribution from SMT scheduling.

Gate simulation also took longer: the interval from the compiled binary's
last write to the final simulation log write grew from 233.74 to 437.46 seconds.
This is a timestamp proxy including process/container transitions, not a
direct measurement of simulator CPU time. Test seeds differed; cycle counts
differed by only 0.7%. Final-GDS LVS was faster on cloud, 295.51 versus 312.18
seconds, so the hardware ranking is workload-specific.

The service's queue pickup, packaging, and durable result publication added
approximately **20 seconds**, separate from the verifier's 146.3 minutes.
That overhead does not explain the regression.

## Matched CPU probe

A fresh diagnostic ran the same pinned amd64 image and deterministic KLayout
geometry operations on both machines. Two regions of 40,000 rectangles were
built before timing; each trial performed three sequences of XOR, merge,
sizing, and subtraction. These operations run in KLayout's geometry engine.
[KLayout documents Regions](https://www.klayout.org/klayout-pypi/overview/geometry/regions/)
as the basis of its design-rule geometry operations.

| Four trials per host | Mac | Cloud |
|---|---:|---:|
| Median wall time | 6.489 s | 9.185 s |
| Range | 6.419–6.519 s | 9.153–9.278 s |
| CPU-quota throttling | None | None |
| Geometry count/area checksums | Identical | Identical |

The cloud took **41.5% longer** on this single-threaded geometry probe.
Process CPU time closely matched wall time. The timed computation had no disk
or network access. This independently corroborates slower CPU execution for
geometry work on the VM; it is not a full-deck replay or an accepted verifier
run. The probe uses the image's KLayout 0.30.9.

Reproduce using [benchmark_eda_geometry.py](../scripts/benchmark_eda_geometry.py):

```sh
docker run --rm --platform linux/amd64 --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 64 \
  --cpus 4 --memory 4g --tmpfs /tmp:rw,size=64m \
  --volume "$PWD/scripts/benchmark_eda_geometry.py:/probe.py:ro" \
  --entrypoint python3 \
  ghcr.io/librelane/librelane:3.1.0.dev3@sha256:d109140b8f17fc54f4fca998beb8124f4949404ec52e339eebd2250854a18b5a \
  /probe.py
```

## Workload equivalence and limits

Both full runs passed, with identical source/configuration hashes, physical
metrics, score, and final netlist/DEF/ODB/LEF bytes. The GDS files are also
byte-identical after ignoring their 50 creation/modification timestamp records.
Different generated layouts therefore do not explain the physical slowdown.
The normalization method and digests are recorded in the JSON report.

These were single historical full runs with different digital-test seeds and
some early competing diagnostics. Historical CPU affinity, frequency, and
quota-throttling traces were not retained. Current idle VM measurements are
not evidence of its historical load. The fresh probe strengthens the CPU
finding without making these runs a controlled whole-system benchmark.

## Next performance work

1. Overlap independent checks on immutable, completed artifacts, retaining every
   acceptance condition. The earlier [scheduling audit](performance-audit.md)
   describes the required failure handling and aggregate resource bounds.
   This targets the long serial schedule and needs a fresh full acceptance run.
2. Select future hardware by measured Magic/KLayout/gate-simulator latency and
   cost per accepted run. Use the probe for preliminary screening, then replay
   actual stages before claiming an improvement or replacing the worker.
3. Measure routing with consistent thread and CPU allowances and physical-core
   placement. This may help routing; the much longer serial checks still remain.

No existing GCP project was changed. The only remote diagnostic execution was
an isolated temporary container on the dedicated verifier VM.
