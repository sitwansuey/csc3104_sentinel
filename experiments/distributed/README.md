# Multi-process fault runs on one host (P3.01)

This harness runs the networked worker layer as separate operating-system
processes, injects network, process and timing faults, and checks invariants
from what the processes left behind. The CI test matrix (ubuntu-latest and
windows-latest) runs its default subset; only Windows runs have been observed
so far, so the Linux-only paths (SIGSTOP in S2, the CA made with Linux openssl)
are not yet verified.

**What it shows.** The Coordinator (`runtime.coordinator_server`) and each
worker (`runtime.worker_service` as planner-1, guard-1, executor-a, executor-b
and verifier-1, plus an Observer feed) run as their own Python process. Each
has its own state directory, its own generated credentials and its own TCP
connections. Each worker reaches the Coordinator through its own fault proxy.
Crashes are real process kills, and a Coordinator restart reopens the same
database file.

**What it does not show.** These are processes on one host. They share one
kernel, one wall clock, one disk and loopback networking. They are not
separate machines or containers. S7 and S7b shift one process's clock with a
gated test offset; every worker measures its offset from the Coordinator
(Cristian's algorithm) and `summary.json` records it under
`clock_offsets_measured`, but on one host that measures the simulated offset,
not skew between machines. The file mode backs Kubernetes with a SQLite file, not
an API server. The Planner is the deterministic provider, so every summary
says `synthetic_planner: true`. The kind mode has not been run yet.

## Run it

From the repository root with the project virtual environment:

```powershell
python -m experiments.distributed.run_scenarios --mode file
python -m experiments.distributed.run_scenarios --mode file --all --repeat 5 --out runs/distributed
python -m unittest tests.test_distributed_processes -v
```

The first line runs the plan's CI subset (S1b, S1c, S2, S4, S5). `--all` adds
S0, S1a, S1d, S1e, S3, S6, S7 and S7b. Each run prints one JSON line and, with
`--out`, writes `<date>-<scenario>[-rN]/` with `summary.json`, `timeline.jsonl`
(every process's log lines and the transitions journal, in time order) and
`coordinator-records.json`. These files hold identifiers, states and counts
only: no credential, lease token, payload or absolute path. The exit code is 0
when every run matched its expectation. A full `--all` pass takes about
two and a half minutes. Set `SENTINEL_DISTRIBUTED_ALL=1` to run every scenario
in the unit tests too (the default subset takes about 75 s).

## Pieces

| Piece | File | Notes |
| --- | --- | --- |
| Topology | `topology.py` | Generates tokens (`secrets.token_urlsafe(32)`) and gives each child only its own, in its environment. The Coordinator gets all of them because it checks them. A throwaway CA and a 127.0.0.1 certificate (openssl, with a private configuration file) give HTTPS; the CA key is deleted after signing. Without openssl the run records `tls: false`. Binds loopback only. |
| Fault proxy | `faultproxy.py` | pass, blackhole, reset, delay and drop-response, per worker, switched at run time. TLS passes through; drop-response lets the handshake finish and then drops the reply. A mode applies to connections accepted after it is set; an open connection keeps its mode. That is a hard partition here only because the worker and approval clients open one connection per request. |
| Fault points | `runtime/fault_points.py` | Named stop points in the worker and Coordinator code paths. Inert unless `SENTINEL_FAULT_POINTS` names an existing directory and `SENTINEL_FAULT_CLUSTER` is a lab cluster (`file-sentinel-distributed` or `sentinel-boutique-connected`). With either variable (or a clock offset) set, a worker starts only if its configured `cluster_id` is that cluster and any configured context is its lab context; otherwise it refuses to start. An armed point fires once: it exits with code 73, like a crash, or blocks until released. |
| File cluster | `tests/support/file_cluster.py` | One Deployment in SQLite, shared by every process. It reuses the simulator's RFC 6902 JSON Patch and etcd-like revision counter (`tests/support/sim_apiserver.py`) and answers the kubectl commands of the production adapter, so `KubectlDeploymentAPI` runs unchanged. Every patch is recorded with its outcome and classified by effect: any patch that writes or changed the pod template is a template write, with or without Sentinel's evidence annotation. Workers accept it only in a lab fault run. |
| Observer feed | `observer_feed.py` | The real Observer state machine and outbox over a synthetic observation series, delivered with the real HTTP ingress. |
| Inputs | `fixtures.py` | The committed samples, moved to the current time and renamed to the file cluster, with a strict live-profile policy and a short health window. |
| Invariants | `invariants.py` | I1-I6 and X1-X4, below. |
| Scenarios | `scenarios.py`, `run_scenarios.py` | S0-S7b, below. |

Process faults: kill uses `Popen.kill()` (SIGKILL on POSIX, TerminateProcess on
Windows). Suspend uses SIGSTOP and SIGCONT on POSIX. psutil is not in the lock
files, so suspend is not supported on Windows: S2 there stalls the Executor at
a blocked fault point and blackholes its link, which also stops its lease
renewals. Workers serve `/healthz` and `/readyz` on 127.0.0.1
(`--health-port`); the harness waits on `/readyz`.

## Invariants

| Id | Holds when |
| --- | --- |
| I1 | At most one committed template write per action, and none without that action's evidence annotation (from the cluster's own record, classified by effect) |
| I2 | No template write after a revocation unless the action went to review |
| I3 | Every incident is RECOVERED, FAILED, NEEDS_REVIEW or REJECTED within the scenario deadline |
| I4 | No delivery has two different acknowledged results |
| I5 | No command is attempted more than 6 times or refused with HTTP 400 more than twice |
| I6 | No Executor ledger ends with a write in doubt (WRITE_STARTED or UNCERTAIN) |
| X1 | No command is claimed by a second identity during another identity's current lease (from the Coordinator's access log, one clock) |
| X2 | No generated credential or lease token in any log, configuration file or argv |
| X3 | Every result a worker saw acknowledged, and every incident the Observer saw acknowledged, is recorded by the Coordinator |
| X4 | Each incident's transitions journal is one unbroken chain ending in its state, and every Executor or Verifier command follows its transition |

I1-I6 follow the A1 design's numbering. X1-X4 are the further properties the
P3.01 task lists (lease exclusivity, no secret in logs, no lost acknowledged
message, journal consistent with the outbox).

## Scenarios and results on this code base

Results from the file mode on one Windows host, three runs per scenario
(`--all --repeat 3`), each run matching the row below. Nothing here has been
run on Linux yet.

| Scenario | Fault | Result |
| --- | --- | --- |
| S0 | none | RECOVERED, 1 write, all invariants hold |
| S1a | Planner killed mid-inference, restarted | RECOVERED, 1 write, all hold |
| S1b | Executor killed after its write intent; peer takes over; crashed host restarted after the incident ends | RECOVERED, 1 write by the peer; **I6 fails**: the crashed host's ledger keeps WRITE_STARTED |
| S1c | Executor killed after its patch committed; peer takes over | RECOVERED, 1 write; the peer adopted it by its evidence annotation (defect (b) of the plan does not reproduce); **I6 fails** as in S1b |
| S1d | Verifier killed mid-collection, restarted | RECOVERED, 1 write, all hold |
| S1e | Executor killed after its write intent, peer stopped, crashed host restarted at once | FAILED with 0 writes: the host fenced its own in-doubt write (P2.06), all hold |
| S2 | Lease holder stalled past its lease before the patch | RECOVERED, 1 write by the peer; the stale host's patch was refused and it adopted the peer's write (defect (c) did not reproduce on this base), all hold |
| S3 | Lease holder's link blackholed after its write intent | RECOVERED, 1 write by the peer, all hold |
| S4 | Response to the Executor's result dropped | RECOVERED; the resent saved body got a replay receipt, all hold |
| S5 | Coordinator killed after committing a result, before the reply; restarted, and restarted again later | RECOVERED, 1 write, all hold |
| S6 | Every worker link blackholed for 12 s while an approval was created | no write and no progress during the partition, then RECOVERED, all hold |
| S7 | Both Executors' clocks 300 s behind (gated offset on the whole Executor clock, including its local Guard re-check), no skew bound, operator approval required | Before P3.03 the write landed but its result was refused with HTTP 400 for the whole run and the incident stayed READY (I3 and I5 failed). Since P3.03 orders results by Coordinator sequence, the result is accepted; the Executor's own Guard re-check sees the Coordinator-stamped approval 300 s in its future and refuses: FAILED, 0 writes, no HTTP 400, all hold, with θ ≈ 300 s measured for both Executors (3 of 3 runs on this branch). It does not recover, because no Executor is within a bound |
| S7b | executor-a's clock 5 s behind, a 2 s skew bound on every worker and the Coordinator | executor-a measures θ ≈ 5 s, stays unready (`clock_skew`) and never claims; executor-b does the work: RECOVERED, 1 write, all hold (3 of 3 runs on this branch) |

S1b and S1c keep the crashed Executor down until the incident ends so that
the peer takes the command every time. In earlier runs that restarted it at
once, whichever Executor claimed first decided the outcome: sometimes the
restarted host fenced its own in-doubt write (FAILED with no write, as in S1e)
or confirmed its own committed write, and I6 then held.

When a known failure is fixed, the matching test in
`tests/test_distributed_processes.py` fails until the scenario's
`expected_failures` is updated, so a fix cannot go unrecorded.

## Kind mode (not yet run)

`--mode kind` accepts only `--context kind-sentinel-boutique-connected` and
refuses every other context, the original lab cluster included. It needs
`--kind-bundle DIR` with `policy.json`, `baseline.json`,
`allowed-targets.json`, a restricted `kubeconfig` and `verifier-inventory.json`
for the target workload. Workers then use the real kubectl readers and the
restricted Executor adapter, the Observer is the real CLI, and template writes
are counted from the Deployment's `metadata.generation` delta and its
`sentinel.io/execution` annotation, independently of Sentinel's records. A
Deployment also raises `metadata.generation` for an annotation-only change, as
the live fence runs showed (`C-EXECUTOR-WEDGE-FENCE`), so an Executor fence
(the harness sets `fence_after_seconds`) would be counted as a write here. The
harness does not inject the workload fault or prepare the bundle; nothing else
may write to the Deployment during a run. This mode has not been run against
the cluster, so no claim rests on it.
