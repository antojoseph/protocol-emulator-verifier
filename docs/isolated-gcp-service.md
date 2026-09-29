# Isolated asynchronous verifier service

**Current status (September 29, 2026):** the experimental `asic-worker-1` VM was
removed at the user's request. Its API and workers are offline. Only that VM in
`asic-verifier-260928-cc9b3c` was deleted; the two disks, private evidence bucket
and checksummed database/configuration backups were retained. Existing projects
were untouched. Retained storage still bills. The deployment measurements below
are historical, and their tunnel commands require restoring a worker first.

New parallel full runs permit two 16 GiB containers concurrently. Provision
64 GiB RAM, including service/OS headroom, using the deployment's `--machine`
option; the original 32 GiB test configuration cannot guarantee both memory
ceilings plus services. A separate, temporary `c4-standard-16-lssd` benchmark VM,
`asic-parallel-east-20260929` in `us-east4-a`, was subsequently provisioned in the
same isolated project. It has 60 GiB RAM and runs standalone verification only;
it does not restore the API, queue or service workers. Its CPU platform is
confirmed as Intel Granite Rapids. The benchmark runs parallel then serial
verification with the same candidate/public seed and unchanged checks. It has
a seven-hour shutdown guard and shuts down when its benchmark script completes.
The original service disks are not attached. Results are pending.

This is an operator-only service wrapping the existing verifier. It does not modify
the candidate, scoring policy, or physical checks, and does not configure any Yukon
production project. A full accepted cloud baseline must be obtained before treating
the deployment as ready for ranked benchmark scoring.

## Isolation and resources

`infra/gcp/deploy.py` creates a **new** `asic-verifier-*` project. It never changes the
gcloud default account/project. Every command has explicit account and project flags.
An existing project is refused. Resumption checks the new project's numeric identity
and random ownership label against the saved local receipt before any mutation.

Resources are confined to that new project:

- One `c3-standard-8` x86-64 worker, 32 GB RAM, Ubuntu 24.04.
- A 30 GB boot disk and 200 GB SSD data disk, both retained when the VM is deleted.
- A separate private VPC. Only IAP's address range can reach SSH; the HTTP API listens
  on localhost. The external IP is used for outbound setup and artifact traffic.
- A private object bucket with public access prevention. The worker can create and
  read objects in this bucket, without project-wide editor or owner access.
- A dedicated PostgreSQL cluster on the data disk, accessed through local Unix sockets
  with peer authentication. It does not use Yukon's database.
- Daily data-disk snapshots retained for seven days, including after disk deletion.
- One fast worker and one full worker, each with a process lock and one active job.

This first deployment is durable across process crashes, reboots, and replacement of
the VM while retaining disks. It is **not highly available**: the database and workers
share a VM/zone. A lost disk requires snapshot recovery (up to one day's queue history
can be lost). Completed evidence is stored separately in the bucket. For multiple
worker VMs, move this project's database to managed PostgreSQL and retain the lease
protocol. Do not place database credentials in candidate containers.

Compute, disks, snapshots, object storage, and networking incur normal GCP charges.
Stopping the VM stops compute billing, but disk and storage charges remain. Provisioning
does not set a billing hard cap or alter existing billing-account budgets.

## Deploy

Authenticate without changing the default account:

```sh
gcloud auth login anto@eigenlabs.org --no-activate
gcloud billing accounts list --account=anto@eigenlabs.org
python3 infra/gcp/deploy.py --account anto@eigenlabs.org \
  --project asic-verifier-UNIQUE-ID --billing-account BILLING_ACCOUNT_ID
```

If required by your organization, add `--organization ORGANIZATION_ID`.
The script records each completed provisioning step under
`.runs/gcp-deployment/PROJECT/receipt.json`. The release is a checksummed archive of
the verifier and hosting code; candidate submissions can never replace that release.
Resume an interrupted deployment with `--resume PATH_TO_RECEIPT`. If a command succeeded
remotely but its receipt write failed, stop and reconcile that resource manually;
the script intentionally does not adopt arbitrary existing resources.

Watch `/var/log/asic-bootstrap.log` using IAP SSH. `ASIC_BOOTSTRAP_READY` means tools,
database, API and worker processes started; it is not proof of full verification.
The pinned tool image and PDK are downloaded once and reused.

## Access and run candidates

The client needs Python 3.9+ and an authenticated `gcloud` tunnel. It does not
need local Docker, PostgreSQL, or EDA tools; those run on the worker.

Open a tunnel (replace PROJECT and ZONE with receipt values):

```sh
gcloud compute ssh asic-worker-1 --account=anto@eigenlabs.org \
  --project=PROJECT --zone=ZONE --tunnel-through-iap \
  -- -N -L 127.0.0.1:8123:127.0.0.1:8123 -o ExitOnForwardFailure=yes
```

In another terminal, from the verifier repository:

```sh
python3 -m service.client health
python3 -m service.client submit candidates/tempo --mode fast --key tempo-fast-first
python3 -m service.client submit candidates/tempo --mode full --key tempo-full-first
python3 -m service.client status JOB_UUID
python3 -m service.client cancel JOB_UUID
```

Save the request key printed before submission; reuse it after a lost HTTP response.
The same key with different candidate bytes, mode, or verifier version is rejected.
The service is an **operator trust boundary**, not a multi-tenant public API. Only
trusted operators should have IAP/OS Login access. A future Yukon integration must
authenticate users, bind jobs to submissions, enforce quotas, and consume authenticated
worker results; simply exposing this API publicly is not that integration.

Requests must use a `127.0.0.1` or `localhost` Host, include `X-ASIC-Client: 1`,
and omit browser `Origin` headers. The Host restriction also prevents a rebound
external hostname from reaching the operator API through the local tunnel.

The API returns queued/running/terminal state, attempt count, candidate and verifier
hashes, result and artifact identity. Seeds and private runtime test files are not
exposed while a job is running. Artifacts are operator-only and may contain runtime
tests; publish only selected completed diagnostics to solvers.

## Correctness and failure behavior

Jobs carry immutable candidate/verifier identities and one random seed shared by all
attempts. Workers renew 180-second database leases every 15 seconds. A reclaimed job
gets a new token; the old worker cannot renew or finalize it. At most three attempts
are allowed following lost leases. Fast and full jobs use separate queues.

Acceptance requires the unchanged verifier's successful exit, matching candidate and
harness hashes, both final integrity checks, every full stage passing, `accepted: true`,
and a finite positive score matching the reported physical area. Fast results always
remain provisional. Evidence upload with a generation precondition completes before
the database can publish acceptance. A cancellation racing with completion suppresses
the result. Completion of the same attempt is idempotent.

The worker imposes a 30-minute fast deadline and a six-hour overall full deadline;
the verifier retains its existing per-stage deadlines, including the four-hour
physical limit. Candidate containers retain network, filesystem, PID, memory and CPU
restrictions. Cleanup targets only containers mounting the specific attempt directory.
Failed cleanup stops the worker. Infrastructure errors never count as acceptance.

On successful artifact upload/finalization, local attempt files are removed. Failed
infrastructure attempts remain for diagnosis. Workers pause with less than 40 GiB
free and abort a run below 10 GiB free or above its 22 GiB scratch budget. Operators
must archive/delete old failed attempts before resuming a capacity-paused worker.

## Operations and verification

Services: `asic-db`, `asic-api`, `asic-worker@fast`, `asic-worker@full`.
Restarting a worker stops its process group; the restarted worker first removes orphan
containers from its own queue's scratch tree. PostgreSQL retains the job, and its lease
allows recovery. No state exists only in an HTTP request or in-memory queue.

Run `python3 -m unittest discover -s tests` for boundary tests. The PostgreSQL tests
require service dependencies and `ASIC_TEST_DATABASE_URL` pointing at an **isolated
throwaway database**; those tests truncate the `asic_jobs` table. Never point them at
the deployment or an existing project database.

Before handoff: verify the private tunnel/API, run real fast and full baseline jobs,
check artifact generation/checksum, exercise running cancellation and worker restart,
and confirm incomplete or negative-control candidates never become accepted. No
finite suite proves all protocols or guarantees Jane Street competition acceptance.

## Deployed instance

The isolated deployment is `asic-verifier-260928-cc9b3c` in `us-central1-a`, with
instance `asic-worker-1` and bucket `asic-verifier-260928-cc9b3c-evidence`.

The cloud Tempo baseline is **accepted**, with the same candidate/verifier hashes,
**515,364 µm²** routed standard-cell area, and score **1.9403761225075868** as the
original accepted baseline. All 75 RTL stages, 75 gate-level stages, nine official
prechecks, timing checks, and both LVS checks passed. The downloaded evidence's
SHA-256 and embedded result match the stored job record.

| Measurement | Observed result |
|---|---:|
| Fast verification | 20.7 seconds, provisional |
| Full verification | 146.3 minutes, accepted |
| Full job through durable result publication | 146.6 minutes |
| Unsafe HDL control | Rejected |
| Running cancellation | Cancelled; no accepted result |
| Worker restart | Recovered and passed on attempt 2 |
| API restart and request boundaries | Passed |

This VM's full run was slower than the earlier 118.8-minute Mac baseline. The
service provides durable asynchronous execution; further full-run latency work
is described in the [performance audit](performance-audit.md). These are individual
runs, not a controlled hardware comparison. No verification checks were reduced.

Use the admin account authenticated during deployment. If the local tunnel is
already open, use the client commands directly; otherwise start it below:

```sh
gcloud compute ssh asic-worker-1 --account=anto-admin@eigenlabs.org \
  --project=asic-verifier-260928-cc9b3c --zone=us-central1-a \
  --tunnel-through-iap -- -N -L 127.0.0.1:18124:127.0.0.1:8123 \
  -o ExitOnForwardFailure=yes

# In another terminal:
python3 -m service.client --url http://127.0.0.1:18124 health
python3 -m service.client --url http://127.0.0.1:18124 \
  submit candidates/tempo --mode fast --key MY_UNIQUE_REQUEST_KEY
python3 -m service.client --url http://127.0.0.1:18124 status JOB_UUID
python3 -m service.client --url http://127.0.0.1:18124 cancel JOB_UUID
```

Use `--mode full` for an acceptance run. Save the returned job UUID and poll it
with `status JOB_UUID`. Completed results include a private `gs://` evidence
location, object generation, byte count, and SHA-256 digest. Download with
`gcloud storage cp` using the same explicit account and project, then verify
the digest before consuming the archive. Do not publish runtime test artifacts.

The always-on estimate is approximately **$350/month**, including the VM, disks,
external IP, and light evidence/snapshot storage. There is no automatic idle
shutdown. Stopping this VM also stops its API and queue processing; retained
disks and bucket storage continue billing. See
[`reports/service-validation.json`](../reports/service-validation.json) for
the cost assumptions, validation status, and deployed release digest.

The temporary organization grants used to create and fund this project were
removed after bootstrap. Organization IAM bindings were compared with the
pre-deployment snapshot and restored. Existing project resources and the CLI's
default account/project were not changed.

## Recovery boundaries

For a process failure, restart only the affected unit. A worker retry waits for
the previous 180-second lease to expire; its immutable source identity and seed
are retained. The maximum is three attempts. Inspect failures before submitting
a new request key, which represents a new evaluation.

For a normal VM reboot, `/data` remounts and the enabled services restart. The
completed bootstrap marker avoids rebuilding the pinned tools. Both disks are
retained if the VM is deleted, but replacing a VM or restoring a snapshot is an
operator procedure, not an automatic availability mechanism. Attach only this
project's retained disk, restore matching service-user ownership, and inspect
the database and tool caches before starting workers. In particular, bootstrap
refuses to overwrite an existing `/data/containerd` cache: on a fresh boot disk,
reconnect `/var/lib/containerd` to that cache while Docker/containerd are stopped.
Never format a retained data disk. PostgreSQL recovers a crash-consistent snapshot
on startup; jobs/results written since that snapshot may be missing, even when
their evidence objects exist in GCS.
