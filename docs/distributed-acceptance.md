# Distributed acceptance on four kind nodes (P4.01)

This is the acceptance run of the networked layer on a dedicated four-node
kind cluster, `sentinel-dist`: the Coordinator and the planner, guard,
executor-a, executor-b and verifier workers (plus the Observer) each run as
their own Deployment, the Coordinator and the two Executors on three
different nodes, under the fault matrix S0-S9 (Chaos Mesh PodChaos,
NetworkChaos and TimeChaos, `docker pause` of a node, fault points armed
inside a Pod, Pod deletion and RBAC, admission and network policy denials).
Template writes are counted from the API server's audit log, which Sentinel
does not write, as invariant I7.

**Status: deployed several times; the first matrix stopped in S1a, no results yet.** The
manifests, the cluster configuration, the audit policy and the harness are
checked by unit tests. The `sentinel-dist` cluster was created and deployed
on 13 Nov 2026, and its first S0 runs repaired nothing: the Guard refused
every proposal as stale and the restore after them failed. After those
fixes a second deployment's first S0 run passed, and the five S0 runs after
it in the same deployment each ended in about 15 s with no incident, because
the harness counted the first run's RECOVERED incident as theirs
([first live attempts](#first-live-attempts)). The first matrix, at
1adcfe3, passed 4 of 5 S0 runs and stopped in S1a r1, because the harness
deleted its PodChaos before Chaos Mesh had acted on it, so the Planner was
never killed
([first matrix](#first-matrix-1adcfe3-a-chaos-object-removed-before-chaos-mesh-acted)).
Both are fixed in code and not yet run live. The results table below is
empty; a later live run fills it.

These are four containers on one Docker host. Each node has its own network
namespace and kubelet, but they share one kernel, one clock and one disk.
They are not separate machines, and nothing here measures skew between
machines: TimeChaos shifts the clock a container reads.

## What is deployed

| Role | Deployment | Node | Coordinator identity | Own Secret | Own PVC | Kubernetes access |
| --- | --- | --- | --- | --- | --- | --- |
| Coordinator | `sentinel-coordinator` | c | (serves 8766 and 8767) | reads every worker's credential, which it checks | `sentinel-dist-coordinator-data` | none (no token) |
| Observer | `sentinel-observer-1` | c | observer-1 | `sentinel-dist-observer-1` | `sentinel-dist-observer-1-data` | read the target, its ReplicaSets, Pods, events and logs |
| Planner | `sentinel-planner-1` | c | planner-1 | `sentinel-dist-planner-1` | `sentinel-dist-planner-1-data` | none (no token) |
| Guard | `sentinel-guard-1` | c | guard-1 | `sentinel-dist-guard-1` | `sentinel-dist-guard-1-data` | get on `demo-api` |
| Executor | `sentinel-executor-a` | a | executor-a | `sentinel-dist-executor-a` | `sentinel-dist-executor-a-data` | get and patch on `demo-api` |
| Executor | `sentinel-executor-b` | b | executor-b | `sentinel-dist-executor-b` | `sentinel-dist-executor-b-data` | get and patch on `demo-api` |
| Verifier | `sentinel-verifier-1` | c | verifier-1 | `sentinel-dist-verifier-1` | `sentinel-dist-verifier-1-data` | get on `demo-api` |
| Target | `demo-api` in `sentinel-dist-workload` | c | - | - | - | - |

The files: the cluster is [kind-dist.yaml](../deploy/kind/kind-dist.yaml)
(node image `kindest/node:v1.37.0` pinned by digest, as kind v0.33.0 lists
it), the audit policy is [audit-policy.yaml](../deploy/kind/audit-policy.yaml),
and the manifests are in [deploy/sentinel/sentinel-dist](../deploy/sentinel/sentinel-dist/kustomization.yaml).
The workers use the [worker image](remote-workers.md#worker-image).

- **Pinning.** Each worker node carries the label `sentinel.io/host=a|b|c`.
  The Coordinator selects c, executor-a selects a and executor-b selects b.
  Node a holds only executor-a and node b only executor-b, so pausing one
  of them stalls exactly one Executor. The target stays on c.
- **Credentials.** The harness creates one Secret per identity with fresh
  random values, sent to `kubectl create` on standard input, never in argv,
  a file or a last-applied annotation. A worker Pod reads only its own
  Secret. The Coordinator reads them all, because it checks them.
- **Planner token.** The default Planner is the deterministic lab planner
  (provider mode, every proposal labelled SYNTHETIC). Following the owner
  note in [remote workers](remote-workers.md#reproduce-and-package), a
  provider-mode Planner needs no Kubernetes credential: its Pod and
  ServiceAccount mount no token and nothing binds it. The optional
  [agent-mode identity](../deploy/sentinel/sentinel-dist/planner-agent.yaml) grants
  read-only `get` on the target and is not applied by default.
- **Executor identity.** Both Executors use the ServiceAccount
  `sentinel-executor`, because the [admission policy](../deploy/sentinel/executor-admission.yaml)
  matches that name; the harness binds it to `sentinel-dist-workload`. The
  audit log tells the two apart by the Pod name bound into each token.
- **In-Pod kubectl.** The Guard and the Executors run kubectl with a
  kubeconfig that names the Pod's projected token file and the cluster CA
  file and holds no credential. Its context is `kind-sentinel-dist`, which
  `runtime/fault_points.py` accepts only for cluster `sentinel-dist`.
- **Probes.** The worker health server listens on 127.0.0.1 only, so
  readiness and liveness are `exec` probes that fetch `/readyz` and
  `/healthz` inside the container.
- **Lab gate.** Every Sentinel Pod runs with the fault-point gate bound to
  cluster `sentinel-dist` (the deterministic planner is accepted only there).
  This is a lab deployment, not a production one.
- **Stale re-plans.** The Coordinator runs with `--stale-replans=3`. The
  tokenless Planner binds each proposal to the incident's resourceVersion,
  which the target's own rollout can move before the Guard checks it. When
  the Guard refuses a ROLLBACK for `STALE_RESOURCE_VERSION` and nothing else,
  while the target's spec is still the one the incident observed, before any
  ALLOW, revocation or seal of that action, the Coordinator reopens the
  incident and sends the version the Guard read back to the Planner, at most
  three times per incident ([first live attempts](#first-live-attempts)).
  The default is 0, so file and kind mode (`run_scenarios`) keep a stale
  refusal final.

### Network policy

Everything in `sentinel-dist` is denied in both directions unless a policy
allows it ([networkpolicy.yaml](../deploy/sentinel/sentinel-dist/networkpolicy.yaml)):

| From | May reach |
| --- | --- |
| Planner | Coordinator 8766; cluster DNS |
| Guard, executor-a, executor-b | Coordinator 8766 and 8767; cluster DNS; the API server |
| Verifier | Coordinator 8766; cluster DNS; the API server; `demo-api` port 8080 |
| Observer | Coordinator 8766; cluster DNS; the API server |
| Coordinator | nothing (it accepts 8766 from the workers, 8767 from the Guard and Executors) |

The plan asked that each worker reach only the Coordinator, and the Executor
also the API server. The Guard, Verifier and Observer read the target through
the API server, and the Verifier samples the target's health port, so they
need those edges too; DNS is needed to find the Coordinator Service. The
shipped API server addresses are TEST-NET placeholders that match nothing;
`deploy` puts in the `kubernetes` Service address (port 443) and the API
server endpoint (port 6443), since a CNI may check either. kind's own CNI,
kindnet, enforces NetworkPolicy; S9 checks that live rather than assuming it.

## Counting writes from the audit log (I7)

The audit policy logs every change to a Deployment or Lease in the two lab
namespaces with its request and response bodies, Secret and token requests
at Metadata level only, and every other request a Sentinel ServiceAccount
makes at Metadata level. `experiments/distributed/audit.py` reads the log and
keeps an allowlisted row per event: no request or response body, source IP,
user agent, credential ID or request URI, a person's user name becomes
`<operator>` and a Secret's name `<secret>`. An extract that still holds
credential material or a Secret name is refused and nothing is written.

A template write is a successful, complete, non-dry-run patch or update of a
Deployment whose JSON Patch writes the Pod template (or a merge patch with
`spec.template`), classified as the file test cluster classifies a patch. So
a fence or Coordinator seal (annotations only), a denied request (403 or 422,
RBAC or admission), a dry run and a read are not writes. I7 passes when
every template write by a Sentinel ServiceAccount comes from the Executor's,
carries its action's evidence annotation, and is the only one for that
action. The lab operator's fault injection and restore are template writes
too, counted apart. The same rows feed I1 and I2, and the Coordinator's
records, the copied ledgers and logs feed I3-I6 and X1-X4
([invariants](../experiments/distributed/README.md#invariants)).

I7 counts only what the API server acknowledged. A successful change of a
Deployment that the log holds without its request body (logged at a level
other than RequestResponse, for example because the audit policy's level or
namespaces are wrong) is `unclassified`, never a harmless metadata write, and
I7 fails on one a Sentinel ServiceAccount made; the operator's are counted
apart (`experiments/distributed/audit.py::classify`). A write the API server
stored but answered with a 5xx or a timeout is `failed`, not a write; the
generation check below is what notices it.

An empty audit extract must not pass as "no writes", so I7 also fails when
the extract may be short (`experiments/distributed/audit.py::completeness`):

- the operator's fault injection is not in it (a positive control for every
  run but S9);
- the target's `metadata.generation`, read before the injection and after the
  restore, rose by a value that no stored change in the extract (template
  write, fence or seal, other metadata write, by anyone) accounts for, or went
  down;
- the earliest event in every audit file read is later than the start of the
  run's window, so earlier events may have rotated out.

The harness reads the live log and every backup the API server rotated it
into (`--audit-log-maxbackup 3`). The window is taken on the cluster's clock:
before and after each run the harness reads the control-plane container's
clock with `docker exec ... date` and estimates the offset as Cristian's
algorithm does (the reading is taken to be at the midpoint, with half the
round trip as its error), then shifts the window by every offset measured and
widens it by the errors (`experiments/distributed/kind_acceptance.py::control_plane_window`).
The offsets are recorded in each run's `summary.json`. A completeness problem
also adds an `unobserved` row to the writes I1 reads, so I1 fails with I7.

## Scenarios

`python -m experiments.distributed.kind_acceptance plan` prints this matrix
and a sample of each Chaos Mesh object, offline.

| Scenario | Fault | File-mode counterpart | Expected |
| --- | --- | --- | --- |
| S0 | none: the harness approves on the operator's behalf through the approval API | S0 | RECOVERED, exactly 1 Sentinel template write |
| S1a | fault point `planner.mid_inference`, then PodChaos pod-kill of the Planner | S1a | RECOVERED, exactly 1 write |
| S1b | fault point `executor.after_start_write`, then PodChaos pod-failure of that Executor until the incident ends | S1b | RECOVERED, exactly 1 write, takeover measured |
| S1c | fault point `executor.after_patch_before_save`, then pod-failure as in S1b | S1c | RECOVERED, exactly 1 write, takeover measured |
| S2 | fault point `executor.before_patch`, then `docker pause` of the holder's node past its lease | S2 | RECOVERED, exactly 1 write, not from the paused holder's Pod |
| S3 | fault point `executor.after_start_write`, then NetworkChaos partition between the holder and the Coordinator (the API server stays reachable) | S3 | every incident terminal, at most 1 write |
| S4 | fault point `executor.after_patch_before_save`, then NetworkChaos partition from the Coordinator to the holder until the result is recorded | S4 | RECOVERED, exactly 1 write |
| S5 | fault point `coordinator.after_commit_before_reply` (exit), then `kubectl delete pod` of the Coordinator | S5 | RECOVERED, exactly 1 write, Coordinator RTO to Ready measured |
| S6 | NetworkChaos partition between the Coordinator and every worker for three leases, approval given meanwhile | S6 | 0 writes during the partition, every incident terminal after it |
| S7 | TimeChaos -300 s on both Executors | S7 | FAILED, none RECOVERED, 0 writes |
| S8 | fault point `executor.before_patch`, revoke the approval, then 8 s NetworkChaos delay on the holder's API traffic | S8 | not RECOVERED, 0 template writes, a seal (fence) from the other Executor's Pod, then a PATCH from the holder's Pod refused with 409 or 422, and no change to the target after the seal |
| S9 | the Executor reads a Secret, patches another Deployment, opens pods/exec and changes `spec.replicas`; the Guard patches the target; the Planner and an Executor open TCP connections they must not | design S9 | every probe as expected (each request denied or timed out, the Verifier's allowed control open), 0 writes |

The Expected column is checked, not just printed: each is a predicate over
the run's incidents, its audit rows and its notes
(`experiments/distributed/kind_acceptance.py::expectation`), and a run passes
only when its invariants hold, its expectation is met, the audit extract is
complete and the harness raised nothing. The invariants alone would let S0
pass with the incident FAILED and no write, or S8 pass without any seal. A
write here is a Sentinel template write in the audit log. S2 and S8 tell the
two Executors apart by the Pod name the API server binds into each token
(both use the `sentinel-executor` ServiceAccount), so without that audit
field they fail rather than pass. In S8 the seal is an annotation change,
which raises the Deployment's generation, so "no change after the seal"
means the target's generation before the operator's restore equals the
generation the seal stored.

S2 here is the stall of the file-mode catalogue, done with `docker pause` of
the holder's node, which is the A6 design's S3; S3 here is the network
partition of the catalogue. S7 expects what file mode showed: with operator
approval required, an Executor whose clock is 300 s behind refuses the
Coordinator-stamped approval, so the action ends FAILED with no write. S0
has only the approved path; the A6 design's S0 also has a Guard-refused path
and an unapproved path, each with no write, which are not in this harness.

Approvals are scripted: in every scenario but S6 a harness thread approves
each action waiting for approval, through the authenticated approval API with
the operator's credential, and in S6 the harness approves once during the
partition. No person reads or types an approval, so these runs show the
approval API and its checks working under faults, not a human in the loop.

Measured in each run: takeover latency (S1b, S1c, S2, S3: from the fault to
the other Executor's first claim, from the Coordinator's access log) and, in
S5, the Coordinator's recovery after the Pod deletion (to Pod Ready, to its
first answered claim and to the next journal transition). The fault time is
taken on the harness's clock and moved onto the cluster's by the offset
measured at the start of the run, since the access log and the Pod's Ready
time come from clocks inside the Docker VM; the result still carries that
offset's error, recorded beside it.

## Live recipe

Run from the repository root in PowerShell, with the project virtual
environment active, Docker Desktop running Linux containers, and kind
v0.33.0, kubectl, Helm and openssl on the path. Do not run it while another
heavy lab experiment is running. `deploy` and `run` hold the shared heavy-lab
lock (`experiments/lab_lock.py`: the file `sentinel-heavy-lab.lock` in the
system temporary directory) for their whole run, as do the P4.03 Chaos Mesh
install and chaos scenario runs ([chaos](chaos.md)) and the P4.05 fault pairs
([dual faults](dual-fault.md)), so one refuses while another holds it. The
lock records the holder, its process id, command and start time, and is
removed when the run ends, also when it fails or is interrupted. `preflight`
only reads it, and also refuses while Chaos Mesh experiments exist in
`kind-sentinel-boutique-connected`, which catches a P4.03 run started
without the lock. A lock left by a process that is no longer running (a run
that was killed) is reported as stale and never taken over silently: check
that nothing of that run is still on a cluster, then add
`--break-stale-lock` to `deploy` or `run`. An empty or unreadable lock file is
treated as held; delete it by hand once you know no run is going on. The
shared lock is tested offline only. Every command
names `--context kind-sentinel-dist` (or `--name sentinel-dist`); none
changes the current context except `kind create cluster`, which the first
block puts back. The harness refuses any other context, and any context or
cluster naming `sentinel-lab` first.

1. Gate and create the cluster (needs about 6 GiB of free Docker memory):

```powershell
python -m experiments.distributed.kind_acceptance preflight --context kind-sentinel-dist --before-create
$previous = kubectl config current-context
kind create cluster --name sentinel-dist --config deploy/kind/kind-dist.yaml --wait 180s
if ($previous) { kubectl config use-context $previous }
kubectl --context kind-sentinel-dist get nodes --show-labels
```

2. Install Chaos Mesh 2.8.2 into the new cluster only, with the project's
   values (containerd socket, namespace filtering):

```powershell
helm upgrade --install chaos-mesh chaos-mesh --repo https://charts.chaos-mesh.org --version 2.8.2 `
  --kube-context kind-sentinel-dist --namespace chaos-mesh --create-namespace `
  --values deploy/chaos-mesh/values.yaml --wait --timeout 10m
```

3. Build the four local images, load them into the cluster and gate the run:

```powershell
docker build -f deploy/docker/coordinator.Dockerfile -t sentinel-coordinator:0.1.0 .
docker build -f deploy/docker/observer.Dockerfile -t sentinel-observer:0.2.0 .
docker build -f deploy/docker/worker.Dockerfile -t sentinel-worker:local .
docker build -t sentinel-demo:0.1.0 experiments/workloads/demo
foreach ($image in 'sentinel-coordinator:0.1.0', 'sentinel-observer:0.2.0', 'sentinel-worker:local', 'sentinel-demo:0.1.0') {
  kind load docker-image $image --name sentinel-dist
}
python -m experiments.distributed.kind_acceptance preflight --context kind-sentinel-dist
```

4. Deploy into a new run directory outside the repository, then check the
   placement (the Coordinator, executor-a and executor-b on three different
   nodes):

```powershell
$run = Join-Path ([IO.Path]::GetTempPath()) ("sentinel-dist-" + (Get-Date -Format yyyyMMddTHHmmss))
python -m experiments.distributed.kind_acceptance deploy --context kind-sentinel-dist --run-dir $run
kubectl --context kind-sentinel-dist --namespace sentinel-dist get pods -o wide
```

5. Run the matrix five times, then print the table:

```powershell
python -m experiments.distributed.kind_acceptance run --context kind-sentinel-dist --run-dir $run --scenarios all --repeat 5
python -m experiments.distributed.kind_acceptance table --run-dir $run
```

6. Collect the sanitised evidence (summaries, timelines, audit extracts,
   records and versions; never databases, keys or certificates), then remove
   the lab. Deleting the cluster is a separate, explicit flag:

```powershell
python scripts/evidence/collect.py --run $run --class distributed-acceptance --bundle-id ("sentinel-dist-" + (Get-Date -Format yyyyMMddTHHmmss))
python -m experiments.distributed.kind_acceptance teardown --context kind-sentinel-dist
python -m experiments.distributed.kind_acceptance teardown --context kind-sentinel-dist --delete-cluster
```

`teardown` first checks that `kind-sentinel-dist` still names the local kind
cluster `sentinel-dist` on a loopback server with its four labelled nodes,
and deletes nothing otherwise, because the admission policy it removes shares
its name with the shipped one. `run` also refuses to start when the target's
UID is not the one `deploy` bound the approval permissions and baseline to.

`run` writes, per scenario and repeat, `<date>/<scenario>-r<n>-<time>z/`
holding `summary.json`, `timeline.jsonl`, `audit-extract.jsonl`,
`records.json` and `versions.json`, and at the top `summary.json` and
`results.md` over every run in the directory. The databases and raw logs it
copies out of the Pods to compute the invariants are deleted after each run.
After each run the operator puts the target's baseline template back when
Sentinel did not, under uid and resourceVersion tests, and records it as an
operator write.

The Coordinator, the Observer and the five workers are long-lived
Deployments with PVCs, so every run of a deployment shares their stores, as
it shares the target. What each run does about that:

- Before it injects, `Lab.begin_run` refuses to start
  (`experiments/distributed/kind_acceptance.py::run_unsettled`) while an
  earlier incident is not terminal (the Coordinator's `planning_blocker`
  would hold this run's incident behind it, and this run's operator could
  approve it), while the Observer's outbox holds an incident it has not
  delivered yet (it would reach the Coordinator during this run and count
  as this run's), while a seal is still required on the target, while the
  target is not healthy at its baseline template, or when the baseline's
  health evidence would pass the policy's `max_age_seconds` (4 h) before the
  run could end, so the Guard would refuse it part way through a matrix.
  Each refusal stops `run` (exit 2); the remedy is to let the incident end
  or to deploy again into a new run directory, which starts every store
  afresh. The Coordinator's and the Observer's stores must be readable for
  this check; a store that cannot be read is not taken to be empty.
- It remembers the incidents the store already holds. Waiting for a state
  or for every incident to end, approving, and the records the invariants
  and the expectation read cover only the incidents opened after that
  (`Lab.incidents`, `scope_records`). S8 waits for a seal newer than the
  ones earlier runs left in `target_ownership`.
- After the restore it waits, with the operator still approving, until the
  target has stayed healthy at its baseline for 20 s, every incident it
  opened has ended and the Observer has delivered every incident it opened
  (`Lab.settle`, up to 240 s), and only then copies the
  records out. A restore, or Sentinel's own rollback, rolls `demo-api` out
  with `Recreate`, and a Pod-less window longer than the Observer's 15 s
  failure window opens an incident of its own a little after the rollout;
  settling keeps that incident in the run that caused it. Each summary
  lists the run's incidents with their opening times, the number of
  earlier incidents left out, the settle result and every approval the
  operator gave (`operator_actions`). A run that does not settle does not
  pass.

What stays across runs and is left alone: the fence epochs in
`target_ownership` (they must keep growing, or the admission policy would
deny later seals: the `sentinel.io/fence-epoch` annotation stays on the
Deployment), the evidence and fence annotations on `demo-api` (the restore
puts back the template only; the next write carries its own evidence), the
Observer's episode checkpoint (a new generation, which every injection
makes, starts a new failure episode, so a new fault opens a new incident),
the Planner's journal and the approvals (both keyed by incident or action),
and the Guard's restart history (the lab policy allows only ROLLBACK). The
Executor ledgers, the transport receipts and worker results, and the
Observer's outbox are read whole, so I4, I5, I6 and X3 check the
deployment's whole history: an earlier run's write left in doubt fails I6
in every later run until it is resolved, which is stricter, not looser. X3
compares the Observer's acknowledged incidents with every incident the
Coordinator recorded, earlier runs' included.

Things a first live run must confirm rather than assume: that the node image
pulls (its digest in `kind-dist.yaml` matches the one the kind v0.33.0
release notes list for `kindest/node:v1.37.0`, compared with the published
notes on 13 Nov 2026, but it has never been pulled; a wrong digest makes
`kind create cluster` fail rather than run another image), that `kind create
cluster` accepts the `apiServer.extraArgs` map in `kind-dist.yaml` (kind may
generate kubeadm v1beta4 configuration for this Kubernetes version, which
takes `extraArgs` as a list of `name`/`value` pairs; if creation rejects the
map, rewrite those four flags in the list form), that kindnet
enforces the network policy (S9's TCP probes) while still letting the kubelet
probe the Coordinator's `/readyz` over its Pod address, that Chaos Mesh's pod-failure
pause image can be pulled into the cluster, that TimeChaos shifts the clock
Python reads in the worker image, that `kubectl port-forward` to the
Coordinator still works during the S6 partition (it did not always: see
[S6 r1](#s6-r1-0b5529f-an-approval-post-cut-in-transit)), that the audit log
carries the Pod name of each ServiceAccount request, and that `date
+%s.%N` and `find` run in the control-plane container.

## First live attempts

On 13 Nov 2026 the cluster was created, `deploy` ran (10:50-10:52 UTC), and
the first S0 runs began. No repair happened. The Coordinator's journal, the
Observer's outbox, the target's events and ReplicaSets and the Pods' logs,
all read without writing, show two incidents, both ending REJECTED with
`STALE_RESOURCE_VERSION`:

| Incident | Opened (UTC) | Planned on | Guard read | Guard verdicts |
| --- | --- | --- | --- | --- |
| first | 10:52:09, generation 3 | resourceVersion 4949 | 5342 at 10:52:16 | REJECT `STALE_RESOURCE_VERSION` |
| second | 10:52:36, generation 4 | resourceVersion 5363 | 5363 at 10:52:41, then 5428 at 10:52:56 | ESCALATE `OPERATOR_APPROVAL_REQUIRED`, approved at 10:52:50, then REJECT `STALE_RESOURCE_VERSION` |

Why the proposals went stale. The Planner binds `based_on_resource_version`
to the incident's `observed_resource_version`
(`services/planner/core.py::propose`). The agent Planner reads the target
again before it saves a proposal (its `finalize` hook), but the lab Planner
has no Kubernetes token by design, so nothing rebinds it, and the Guard
compares it with its own live read
(`services/guard/core.py::_state_policy_reasons`), as it must. `demo-api`
rolls out with `Recreate`: the old ReplicaSet goes to 0 first, and the new
Pod is created only once the old one has gone, about 30 s later here (the
Pod's termination grace period). The Observer's 15 s failure window
therefore opened each incident while no Pod existed. The Deployment
controller then wrote the Deployment's status again when it scaled up the
new ReplicaSet (replicas, updatedReplicas, unavailableReplicas and the
Progressing condition), and would again at the 90 s progress deadline. A
crash-looping Pod alone changes Pod status, not the Deployment. For the
second incident that write came at 10:52:49, after the Guard's first pass
and before its re-check after the approval. The Phase 3 kind runs against
the Boutique's `shippingservice` did not meet this: that Deployment uses the
default RollingUpdate, so its new Pod already existed when the incident
opened (not re-checked live).

The first incident was not the operator's fault injection. `deploy` applied
the whole overlay last, `workload.yaml` included, at 10:51:41, after it had
pinned the demo image by digest and recorded the baseline. That apply put
the tag image back, so the target left its baseline and rolled out again,
and the Observer, started by the same apply, opened an incident for that
rollout. The operator's injection followed at 10:52:19, on the tag-image
template.

The operator's restore after the runs never happened: `Lab.restore` sent its
JSON patch with `kubectl patch --patch-file=-` and the patch on standard
input, and kubectl on Windows looked for a file named `-`.

What changed:

- The Coordinator can re-plan after a stale refusal
  (`services/coordinator/core.py::replan_after_stale`, `--stale-replans`,
  3 on this lab, 0 by default). It acts only on a Guard REJECT whose only
  reason is `STALE_RESOURCE_VERSION`, for the incident's active ROLLBACK, and
  only when all of these hold (`services/coordinator/core.py::_replan_declined`):
  - The target's spec is the one the incident observed. With a stale
    refusal the Guard records in its report the live `metadata.generation`
    and template digest it read (`STALE_READ_SPEC`, status SKIP, never a
    reason code; `services/guard/decisions.py::_core_findings`), and that
    generation must equal the incident's `observed_generation`. A status
    write moves the resourceVersion and leaves the generation; a change to
    the spec (an operator's fix, a new deploy, another controller's write)
    moves both, and a ROLLBACK planned from the older evidence would then
    overwrite it. The first proposal's version binding guaranteed that the
    spec had not changed since the evidence; this check keeps that guarantee
    for the re-plan. Without the record the refusal is final. By the
    timeline above, both live refusals were status churn on the incident's
    generation: the first Guard read (10:52:16) came before the injection
    (10:52:19) that made generation 4, and the second incident's generation
    4 had no later spec change before its re-check (the ReplicaSet created at
    10:52:49 is the injected template's). The 13 Nov reports predate the
    record, so this is read from the timeline, not from the reports.
  - No approval of the action was revoked, it was never ALLOWed (READY), and
    no seal was required for it. A re-plan belongs to the proposal's first
    approval cycle. After a revocation, or once an Executor command existed,
    the stale refusal stands, as P3.04 has it for a sealed action: the
    version it was planned on is gone.

  It keeps the refused action REJECTED with its decision, moves the incident
  back to OPEN (journal cause `stale_replan`) and queues a `planner.request`
  that carries the recorded incident and the resourceVersion the Guard read.
  The Planner plans it under its own journal key, so it is a new proposal
  with a new action ID and a rationale that says it is a re-plan (provider
  and agent mode). The Guard checks it against live state again, the
  Executor's patch still tests that version, and the operator must approve
  it again, because an approval is bound to the proposal's digest. After the
  last re-plan a stale refusal is final, and a declined re-plan is logged as
  `coordinator.replan.declined` with its reason. The Planner still has no
  Kubernetes token, and the Guard's check is unchanged. Between the reopening
  and the new proposal the incident is OPEN while its active action is still
  the refused one; an earlier OPEN incident holds later incidents on the same
  workload (`planning_blocker`), so recoveries stay serialised.
- How the other scenarios meet the rule on this lab. S8 revokes the approval
  while the holder waits before its patch; the other Executor's seal PATCH
  and the rollout's status writes move the resourceVersion, so the
  re-check after the revocation is refused stale and that refusal is final
  (revoked, and ALLOWed before). S8 still expects no write. S6 approves its
  first action by hand during the partition; if the re-check after the
  partition is refused stale (the progress-deadline status write comes about
  90 s after the scale-up), the re-plan waits for approval, so S6 now
  starts its operator once the partition is removed
  (`auto_approve: after_fault`, `kind_acceptance.AFTER_FAULT`). Its
  expectation (no write during the partition, every incident terminal, at
  most one write) is unchanged. S1b, S1c, S2, S3, S4 and S5 inject their
  fault after the ALLOW, so a stale refusal the fault causes stays final, as
  before. Every scenario but S6 and S9 approves from the start, so a re-plan
  in the first approval cycle is approved like any action waiting for
  approval. These are offline analyses and tests; none has run live.
- `deploy` applies the overlay without `workload.yaml`, which it applied and
  pinned first, and stops if the target's template changed after its
  baseline was recorded.
- `Lab.restore` writes the patch, which holds no credential, to a private
  file in the run directory, passes that path and deletes the file
  afterwards. No other harness command reads a patch from `-`; `kubectl
  apply` and `create` read `--filename=-` from standard input on every
  platform.

Still to run live: rebuild the Coordinator and worker images, deploy into a
new run directory and run S0 again. A second incident may open after a
successful rollback, while the rollback's own `Recreate` rollout has no Pod;
that has not been seen here yet. A status write between a re-plan's ALLOW
and the Executor's patch (for example the progress-deadline write) still
makes the patch fail its resourceVersion test: the action ends FAILED and
is not re-planned, so S0 can still end without a repair. (That happened in
the [third deployment](#third-deployment-recreate-and-conflicts), which
changed it.)

### Second deployment: one pass, then runs that waited for nothing

Later on 13 Nov 2026 the lab was deployed again into a new run directory
(12:53-12:55 UTC) with the harness at 2ef594a, and S0 was run once, then
five times in a second `run` invocation against the same deployment. The
run summaries, the Coordinator's journal and messages, the Observer's poll
log and the target's managed fields, events and ReplicaSets, all read
without writing, show:

| Run (start, UTC) | Duration | Incidents in scope | Sentinel template writes | Operator restore | Result |
| --- | --- | --- | --- | --- | --- |
| S0 r1 (13:01:15) | 57.8 s | 1, RECOVERED | 1 | no | passed: every invariant, expectation met |
| S0 r1-r5 of the second invocation (13:02:36-13:03:35) | 12.6-16.1 s each | none | 0 | yes | failed I3 (no incident) and X3 |

The first run did what S0 is for. The Observer opened `INC-082C30CA9DF1`
at 13:01:32, 16 s after the injection (the injected template's `Recreate`
rollout left no Pod from 13:01:16), the operator approved it at 13:01:41,
the Guard allowed it, executor-a rolled the template back at 13:01:44 and
the Verifier found the target healthy: RECOVERED at 13:02:05, one template
PATCH in the audit log. No stale refusal happened.

Each later run restored the target 11 to 15 s after its injection. The
harness's wait asked whether every incident in the Coordinator's store was
terminal, and the store, which outlives runs on its PVC, still held the
first run's RECOVERED incident, so the answer was yes at once
(`Lab.terminal` at 2ef594a). The harness then copied the records, restored
the target before the Observer's 15 s failure window could fill, and found
no incident of its own. The Observer's poll log agrees: in each run it read
no Pod of the target for only about 8 to 12 s, its incident count stayed 1
through all five runs, and it delivered nothing. In the runs whose events
were still listed when they were read (from 13:03:19), the restore came
while the old Pod was still terminating, so no Pod of the injected template
started. X3 failed as well, falsely: the
Observer's outbox, read whole, holds the first run's delivered incident,
which the Coordinator did record, but X3 compared it only with the run's
own (empty) set of incidents.

After the fifth run's restore (13:03:50) the target's managed fields show a
further write by `kubectl set` (the manager the harness's injection uses) at
13:03:51 and a `kubectl patch` putting the template back at 13:04:13, with no
evidence folder for either. The operator's session notes account for both: the
first is the injection of the matrix's next run, which the operator stopped
before it wrote a summary, and the second is the operator's manual restore
(a uid- and resourceVersion-tested JSON patch from a file) after stopping the
matrix. In the 21 s between
them the target had no Pod, and the Observer opened `INC-ECED0043B19F`
at 13:04:08 for that generation (15), after the first incident had
RECOVERED: a new fault on the same target opens a new incident, as the
Observer's episode per generation intends
(`services/observer/core.py::Observer.observe`). It was planned, and the
Guard escalated it for approval at 13:04:10, before the patch. With no
operator left it is still `AWAITING_APPROVAL`, with the target healthy at
its baseline (generation 16). Through the Coordinator's `planning_blocker`
it holds every later incident on `demo-api`, so any further run in this
deployment would have opened an incident that could never be planned.

What changed (offline, `tests/test_kind_acceptance_repeats.py`): each run
starts only on a settled lab (no earlier incident open or undelivered),
remembers the incidents already in the store,
waits for, approves and checks only the ones it opens, and settles after its
restore before it copies anything, as described under
[live recipe](#live-recipe); S8 waits for a seal newer than earlier runs'.
X3 compares the Observer's acknowledged incidents with every incident the
Coordinator recorded. The symptom test, a store holding an earlier RECOVERED
incident, gives states `[]` at 2ef594a and `RECOVERED` now. Under the new
harness the deployment above would be refused before injecting, because of
`INC-ECED0043B19F`; it was left as it is, read only. The next live attempt
deploys again into a new run directory (or lets an operator decide that
incident first) and runs S0 several times in one deployment.

### Third deployment: Recreate and conflicts

On 13 Nov 2026 (16:38-16:45 UTC) the lab was deployed again, with run
scoping fixed, and S0 was run three times in that deployment. The
Coordinator's journal and messages, verifier-1's store and the Executors'
logs, all read without writing, show:

| Run | Incident opened (UTC), generation | What happened | Sentinel template writes | Incident ends |
| --- | --- | --- | --- | --- |
| r1 | 16:39:13, 3 | stale refusal, re-plan, approval, ALLOW, executor rolled back at 16:39:54 (generation 4) | 1 | NEEDS_REVIEW (outcome UNKNOWN) |
| r2 | 16:42:30, 5 | approval, ALLOW at 16:42:41, executor-a: `CONFLICT` "Conditional write was refused without committing" at 16:42:44 | 0 | FAILED |
| r3 | 16:43:48, 7 | approval, ALLOW at 16:43:55, executor-b: `CONFLICT` "Target identity or resource version changed" at 16:44:20 | 0 | FAILED |

r1 repaired the target and was still not counted. verifier-1's first reading
came 6 s after the write, during `demo-api`'s `Recreate` rollout, when no Pod
existed: the HTTP probe got no response and the Deployment reported 0 ready
and 0 available replicas. Every later reading, from +13 s to +84 s, was
healthy, with the approved template. The outcome was UNKNOWN with only
`service_health` failed. The convergence grace (60 s here) set aside only a
leading run of complete, unhealthy readings, and it judged a start only when
the whole window was otherwise complete (`services/verifier/core.py::verify`
at b9e4ddb); a reading with no response is incomplete, not unhealthy, so the
Pod-less reading kept the whole window UNKNOWN.

r2 and r3 wrote nothing, correctly. Between the Guard's ALLOW and the
Executor's patch the Deployment controller wrote the target's status again
(the injected template's `Recreate` rollout and its progress condition), so
the resourceVersion moved: in r3 the Executor's own read already showed
43314 for a proposal on 43248, in r2 the adapter's read just before the
patch refused it (the proposal was on 43022). The generation each incident
observed (5 and 7) was still the target's until the operator's restore
after the run (6 and 8 follow from those restores; read from the timeline,
not from a recorded read). CONFLICT was final for the incident, so neither
run repaired anything. The stale re-plan of the first live attempts covers a
refusal by the Guard, not this one, as noted under
[first live attempts](#first-live-attempts).

What changed (offline):

- **The Verifier: a reading during the rollout counts as not yet
  converged.** The live collector records with every reading the
  Deployment's `generation`, `observedGeneration`, `updatedReplicas` and
  `status.replicas` (`total_replicas`), bounded integers from the same
  consistent Deployment read as the readiness counts
  (`services/verifier/live.py::LiveEvidenceCollector._snapshot`); a failed or
  changing read records none of them. Inside `convergence_grace_seconds`, a
  reading with no response (no HTTP status, no gRPC status and check, no
  Redis reply), 0 ready and 0 available replicas and counts that show the new
  template not yet in place is judged as the unhealthy reading it is:
  `observed_generation` below `generation` (the controller has not yet acted
  on the write) or `updated_replicas` below `desired_replicas` (fewer Pods of
  the new template than the spec asks for, which includes no Pod at all, the
  updated 0 / total 0 a Recreate rollout reports before the new Pod starts)
  or `available_replicas` below `updated_replicas` (a new Pod exists but is
  not yet available: the rollout is not finished in the sense `kubectl
  rollout status` uses) (`services/verifier/core.py::_awaiting_rollout`).
  The third test was added after the fourth deployment's S0 r3 (614c4aa)
  ended NEEDS_REVIEW on a first reading taken while the new container was
  still starting (+5 s: updated 1, total 1, ready 0, no HTTP response); it
  cannot by itself give RECOVERED, because the readings after it must still
  pass every rule. Counts that contradict each other never qualify. Every other fact of that
  reading must still be complete and right (target identity, timestamp,
  approved template, `configuration_stable`, dependency evidence); the rest
  of the grace rule is unchanged: only a leading run inside the grace is set
  aside, only when the readings after it alone pass every rule, a relapse
  counts, readings after the grace count, and the outcome's `convergence`
  check names the cause (`service not answering during rollout`). When the
  readings after the set-aside run do not recover, the verdict is the strict
  one on the readings as they are, as before: UNKNOWN (NEEDS_REVIEW) for a
  target that never answers or that answers unhealthy after a silent reading,
  NOT_RECOVERED for one that answers unhealthy at every reading. A reading
  from a collector that does not record the counts
  (the Boutique harness and file mode) is judged exactly as before.
- **The Coordinator: a CONFLICT that provably wrote nothing is re-planned.**
  The Executor's CONFLICT result now carries an optional `conflict` record
  when, after refusing, it read the target and the decision table showed the
  same UID at a version other than the proposal's, without this action's
  evidence or fence: `basis` (NOT_SENT: it sent no write, its own read showed
  another version; REFUSED: the adapter refused the patch's preconditions
  without committing), the `resource_version` it read and the Deployment's
  `generation` (`services/executor/core.py::_conflict`, contract in
  `shared/contracts.py`). With `--stale-replans` above 0
  (`replan_after_conflict`), the Coordinator then reopens the incident
  (journal cause `conflict_replan`) and queues a `planner.request` carrying
  that version and the cause `EXECUTOR_CONFLICT`, only for the incident's
  active ROLLBACK, only when the generation read equals the incident's
  `observed_generation`, only if the Executor reported nothing earlier on the
  action (no write ever in doubt), only in the action's first approval cycle
  (one approval at most, never revoked, one ALLOW) and only with no seal for
  it; the budget is the stale re-plan's, shared over both causes. The new
  proposal has a new action ID and a rationale that says why it was
  re-planned; the Guard checks it against live state, the operator approves
  it again and the Executor's patch tests its version. A CONFLICT without the
  record (an older Executor, an unreadable target, a replaced target, a
  write that was ever in doubt, FENCED included) stays final, as does any
  CONFLICT with the default `--stale-replans=0`.

Why these and not the alternatives:

- *Wait before the first reading instead* (start sampling only once the
  controller has observed the new generation and an updated Pod exists,
  bounded by the grace). Rejected. It would cover no more than the rule
  above: after the wait the first reading can still find an updated Pod that
  does not answer yet, which both leave as missing evidence, and waiting for
  an answering Pod is waiting for health, which is the verdict's job, not a
  precondition's. It would also
  move part of the judgement into the sampling loop
  (`runtime/remote_adapters.py::VerifierAdapter._sample`), where the fixed
  deadline, the lease checks and the restart-from-saved-readings live, and
  the seconds spent waiting would leave no reading in the store or the log.
  Judging the reading in `verify()`, a pure function, keeps every reading
  saved and logged, keeps the decision in one place that the window tests
  already cover exhaustively (`tests/test_verifier.py`), and discloses each
  exclusion in the outcome.
- *Let the Executor re-read and re-guard after a CONFLICT instead.* Rejected
  as less safe. The proposal, its approval (bound to the proposal's digest),
  the Guard's ALLOW (`validated_resource_version`) and the patch's
  precondition all name one version, V0. Writing at a newer version under the
  same action would need either a Guard decision and an approval for a
  version no one approved, or an Executor that makes its own proposal. It
  would also break what reconciliation, fencing and the seal rest on: every
  write of an action tests the same V0, so once the version has left V0 no
  write of the action can ever commit
  (`services/executor/reconciliation.py`; `runtime/worker_transport.py` needs
  no seal after a CONFLICT for that reason). A second write at V1 under the
  same action ID could still commit after an earlier holder's result, or a
  seal fenced at V0, had declared the action unable to commit. A re-plan is a
  new action with its own version, journal row, Guard decision, approval and
  seal scope, through a path already reviewed for the stale refusal.
- *Why the generation is the right test.* Kubernetes raises a Deployment's
  `metadata.generation` for any change to its spec or its annotations made
  through the Deployment itself: this action's write (template and evidence
  annotation), a fence's marker, an operator's fix. The controller's status
  writes go through the status subresource and raise neither. Equal to the
  incident's `observed_generation` means nothing but status changed since the
  incident, so no write of this action (nor anyone's fix) landed, and a
  ROLLBACK planned from the incident's evidence cannot overwrite a newer
  spec. The re-planned proposal is bound to
  the version the Executor read: if the spec changes after that read, the
  version moves and the Guard refuses the proposal as stale, which the stale
  rule then refuses to re-plan (SPEC_CHANGED).

What it costs: each re-plan needs the operator's approval again, and on this
lab the controller can still write status between a re-plan's ALLOW and its
patch, so S0 can still end FAILED after the last re-plan (3 per incident,
both causes together). How the other scenarios meet the rule (offline
analysis, not run): S8 is unchanged (revoked, and sealed: final). In S7 a
re-planned action meets the same clock refusal (FAILED, which is never
re-planned), so it still ends FAILED with no write. In S3 a takeover holder
that finds the first holder's write adopts it (APPLIED); one that reports
CONFLICT with the record proves that write can no longer commit (the version
left V0) and has not (the generation is the incident's), so a re-plan keeps
S3's bound of at most one write. S1b, S1c, S2, S4 and S5 expect the one
write they already make.

Tests: `tests/test_verifier_rollout.py` (the r1 readings, saved by
verifier-1, recover once the Pod-less reading carries its rollout counts
and stay UNKNOWN without them; the same timeline through the real delivery
layer, `RemoteWorker` and `VerifierAdapter`; every other gap in the silent
reading still counts; a silent reading once every desired Pod of the new
template exists, or with contradictory counts, still counts, and each sign of
an unfinished rollout alone is enough; a silent reading after the grace or
after a healthy one counts; a target that never answers, or answers 503 after
a silent start, ends UNKNOWN, and one answering 503 throughout NOT_RECOVERED;
gRPC and Redis; the collector's counts) and `tests/test_conflict_replan.py` (r3 and r2
over loopback HTTP with the real Planner, Guard and Executor workers: the
CONFLICT is re-planned on the version the Executor read, approved again and
repaired with one write; a spec change before or inside the patch, a
replaced target and the default setting leave it FAILED; the budget is
shared with stale re-plans and every re-plan has its own Planner row; the
Coordinator's rule declines without the record, for a moved spec or an
unmoved version, after an earlier result, a revocation, a second approval
or ALLOW, a seal, for a RESTART and past the budget; the Executor's record
and the contract). At b9e4ddb the symptom tests end UNKNOWN / NEEDS_REVIEW
(r1) and FAILED with no write (r2, r3).

Still to run live: rebuild the Coordinator and worker images, deploy into a
new run directory and run S0 several times in one deployment, then the
other scenarios.

### First matrix (1adcfe3): a chaos object removed before Chaos Mesh acted

On 13 Nov 2026 the lab was deployed again (its Pods created at 20:48 UTC)
and the whole matrix was started at 1adcfe3 (`--scenarios all --repeat 5`).
S0 passed 4 of 5; the fifth ended with a kubectl write in doubt that was
resolved as SUPERSEDED, a known limit of the kubectl transport. The matrix
stopped in S1a r1. The Planner's log and fault directory, its Pod, the
namespace's events and Chaos Mesh's controller log, all read without
writing at 21:10 UTC, show:

| Time (UTC) | What happened | Read from |
| --- | --- | --- |
| 20:59:02.146 | the Planner reached `planner.mid_inference` (block) | Planner log `fault_point.reached`, `/faults/planner.mid_inference.reached` |
| 20:59:15.024 | Chaos Mesh validated the create of PodChaos `s1a-r1-planner` | controller log |
| 20:59:15.668 | the controller's `initFinalizers`: "unable to get chaos ... not found", then "fail to update" at 15.742 | controller log, event `Failed to update finalizer` |
| 21:09:02.093 | the Planner went on only when the fault point's own limit (`MAX_BLOCK_SECONDS`, 600 s) ran out, with no `.release` written | Planner log `fault_point.released` |

The Planner's Pod kept its UID and had 0 restarts: it was never killed.
Its incident stayed OPEN past the deadline (562 s), the settle failed, and
the next run was refused, correctly (the earlier incident OPEN, as
`planning_blocker`).

Why: at 1adcfe3 `s1a` created the PodChaos (`kubectl apply`), dropped its
hold on the Planner (`run.held.clear()`), and ran `kubectl rollout status`
on the Planner's Deployment, which returned at once because nothing had
happened to it yet. `s1a` then returned, and `run_one`'s `finally:
run.cleanup()` deleted every chaos object still listed, about 0.6 s after
it was created and before the controller had set its finalizer. With the
hold already dropped, the cleanup did not release the Planner either.
Nothing in the harness checked that a fault was in effect, so every chaos
scenario had the same gap even where the timing hid it: S1b and S1c waited
for the incident under a pod-failure that might not have been injected
yet, S3 and S4 released the holder before their partition, S6 approved
before its partition, S7 waited under a skew not yet applied, S8 released
the holder before its delay, and S2 trusted `docker pause`'s exit status.

What changed (offline, `experiments/distributed/kind_acceptance_live.py::Run`
and `experiments/distributed/kind_acceptance.py::chaos_effect`):

- **Selection is read before creation** (`Lab.chaos_targets`). Every
  selector must name `sentinel-dist`, which must carry
  `chaos-mesh.org/inject=enabled` (Chaos Mesh runs with
  `enableFilterNamespace`, so without it nothing is injected), every app it
  names must have a running Pod, a mode-one fault must have exactly one
  candidate, and a fault aimed at the holder of a fault point must select
  that holder's Pod (by UID). When the hold could not read that Pod, the
  fault is not created at all (a harness error), rather than created
  without the check.
- **A fault is confirmed in effect from the cluster before the scenario goes
  on** (`Run.create`, `Lab.confirm_chaos`, polling every second for up to
  `CHAOS_CONFIRM_SECONDS`, 60 s). For pod-kill: no Pod it selected is still
  running (its UID gone or being deleted). For pod-failure, partition,
  delay and TimeChaos: `desiredPhase` Run and a record Injected for every
  Pod it selected (both ends of a partition, selector keys `.` and
  `.Target`; each named container for TimeChaos), and for pod-failure the
  container that was running in the selected Pod no longer is. The live
  controller sets `desiredPhase` Run almost at once, before it selects or
  injects anything, so Run with no records, or with a record not yet
  Injected, is still waiting; every record Injected under a `desiredPhase`
  other than Run (Stop, Pause) is not in effect either. A record for
  a Pod the harness did not select, in another namespace or for another
  container is an error at once. When confirmation does not come in time
  the run stops with a harness error naming what was missing, and does not
  pass. `docker pause` and `unpause` are confirmed by the node's
  `State.Paused`.
- **A hold ends only on evidence.** S1a drops its hold on the Planner only
  when the confirmation shows the holder's Pod UID killed, and S1b and S1c
  only when it shows the holder's container replaced (`Run.fault_ended`).
  S1a then waits up to 180 s for the replacement Pod to be Ready before it
  removes the object.
- **A fault is removed only after its window.** Waits under a duration
  fault are bounded by what is left of its duration (`Run.window_left`).
  Removal reads the object again first, deletes it with `--wait` and checks
  that it is gone; a fault that Chaos Mesh had already ended (its duration
  ran out) is a harness error, since it did not cover the scenario's
  window. A delete that succeeds while the object is still there is an
  error, not a removal. S6's partition lasts three leases plus 60 s plus
  `CHAOS_CONFIRM_SECONDS`, so three leases still fit after the slowest
  confirmation; S6 watches for no longer than the partition has left
  (`notes.during_partition.seconds`), and a watch shorter than three leases
  is a harness error. A write during the partition is reported together
  with, not instead of, a failed removal.
- **Nothing is left behind silently.** `Run` tracks every fault point it
  armed and every process it holds. `cleanup`, in `run_one`'s `finally`,
  unpauses nodes, removes chaos objects, releases held processes (or
  records that their Pod or container went), disarms points no process
  took and releases any process that took one without being held. Each
  step is an operator action in `operator_actions`; a step that fails is a
  cleanup problem, added to the harness error. `Lab.settle` also reads the
  cluster again for what the run's faults left (`Run.leftovers`: a chaos
  object still there, a node still paused, a point still held or armed)
  and does not settle while there is any; the summary reports it under
  `settle.leftovers` and `cleanup`. `run` refuses to start while a chaos
  object with the harness's label is in `sentinel-dist`, and stops the
  matrix after a run whose cleanup reported a problem or whose settle found
  a leftover, so the next run never starts on top of it. A release that
  needed retries records both the Pod the hold recorded (`pod`) and the
  one running when the release succeeded (`released_in_pod`). An
  unexpected `LookupError` in a scenario runner is recorded as the harness
  error with the cleanup and settle, like the other harness errors.
- **The evidence is in each summary**: under `notes.chaos`, the Pods each
  selector matched, the confirmation (records and their phases,
  `desiredPhase`, the killed Pods or replaced containers, when it was
  confirmed and how long it took), the replacement Pod, when the object
  was removed and whether it was still in effect then; under `notes.pause`
  the confirmed pause and unpause; under `notes.fault_point_ended` the
  holds a confirmed fault ended.

Selectors and the namespace annotation were checked: every chaos object
selects Pods only in `sentinel-dist`, by their `app` label, which is the
Deployment's name and its Pod template's label in the manifests and on the
live Pods. `sentinel-dist` carries `chaos-mesh.org/inject=enabled`;
`sentinel-dist-workload` does not, and no chaos aims at it, so that is
right (S8's delay reaches the API server by `externalTargets`, an address,
not a Pod selector). The harness makes no StressChaos (`chaos` refuses it);
`teardown` and the leftover check still look for one.

To confirm live, not assumed: that Chaos Mesh 2.8.2 records both ends of a
partition (keys `.` and `.Target`, from its NetworkChaos selector specs;
the P4.03 runs saw only source-side records, for a delay without a
target). If it records only the source, every S3, S4 and S6 run stops with
"not confirmed in effect" naming the target Pods rather than passing. Also
that pod-failure shows as a new container ID (or a stopped container) in
the Pod's status, and that a pod-kill's replacement is Ready within 180 s.
If the harness process itself is killed, its cleanup does not run: a chaos
object may stay (the next `run` refuses to start until it is removed), a
node may stay paused, and a fault point stays blocked until its own 600 s
limit.

Tests: `tests/test_kind_chaos_confirmation.py`, with a fake cluster whose
Chaos Mesh selects after half its delay and injects after all of it, as the
live controller does. For each fault kind (pod-kill in S1a, pod-failure in
S1b and S1c, partition in S3, S4 and S6, delay in S8, TimeChaos in S7,
`docker pause` in S2): the scenario goes on only once the fault is in
effect, and a fault that never takes effect stops the run with a harness
error and leaves nothing behind. Also: a fault that ended before its
removal is an error; an unannotated namespace, a selector with no running
Pod or one that would not select the holder is refused before anything is
created; a record for a Pod the harness did not select is an error, not a
wait; `desiredPhase` Run with no records (the fake controller sets Run at
creation, as the live one does) or with every record Injected under Stop or
Pause is not in effect; the selectors match the manifests' Pod labels and
only `sentinel-dist` is annotated; a chaos object that cannot be removed,
or is still there after a successful delete, is a cleanup problem and keeps
the lab from settling; a point no process reached is disarmed, and a second
process that took it is released; a hold drops only for the Pod UID or
container the confirmed fault ended; a fault aimed at a holder whose Pod
could not be read is not created; S6 watches three leases after the slowest
confirmation, says when its partition has less left, and reports a write
during the partition with a failed removal; `run_one` records the failed
confirmation, every cleanup step, a cleanup problem in the harness error
and an unexpected `LookupError`; and `run` stops the matrix after a run
that left a fault behind. At 1adcfe3 every one
of these tests fails, the S1a one because the PodChaos is deleted before the
fake controller injects it and the Planner is left blocked.

### S2-S9 matrix (b37017e): reads while the Coordinator Pod was replaced

On 14 Nov 2026 the matrix S2-S9 was started at b37017e on
`kind-sentinel-dist`. S2, S3 and S4 each passed 5 of 5. S5 r1, which
restarts the Coordinator and then deletes its Pod, did not finish. The
harness's output showed a traceback from the operator thread: through
`approve_pending`, `incidents`, `all_incidents`, `query` and `exec_python`,
a `kubectl exec` into the Coordinator's Pod while that Pod was being
replaced, ending in `AcceptanceError: kubectl --context=kind-sentinel-dist
failed (exit 1)`, which ended the thread. The main thread then hit the same
error and the run stopped with `Refused: kubectl --context=kind-sentinel-dist
failed (exit 1)`. No S5 summary was written. The cleanup ran: the target
was back at its baseline and the heavy-lab lock was released.

What is known and what is not. kubectl's stderr was not kept, and the
message named only the context flag (the first argument), not the verb or
the reason, so which read failed in the main thread, and whether it failed
because the Pod was gone, its container was not running or the kubelet
refused the connection, is not known beyond exit status 1 and the time.
Read on its own, the code at b37017e did not explain the operator thread's
traceback: that thread suppressed `AcceptanceError`, and `Lab.query`
returned no rows for any failed read unless `strict`. The likely
explanation, found after the next S5 stop
([below](#s5-r1-refused-at-its-first-poll-one-module-run-twice)), is that
the harness ran as two modules. Run with `python -m`, `kind_acceptance.py`
is `__main__`; `main()` built its `Shell` there, so a failed kubectl call
raised `__main__`'s `AcceptanceError`, while the operator thread's
`suppress` and `Lab.query`'s `except` named the class of the
`kind_acceptance` module that `kind_acceptance_live` imports by name.
Neither matched, so the operator thread died with the traceback and the
main thread's read raised instead of returning no rows. This fits every
part of the output kept, and the traceback as quoted names the class with
no module, which is how Python prints a class defined in `__main__` (the
imported module's would print as
`experiments.distributed.kind_acceptance.AcceptanceError`); it is not
proved, because kubectl's stderr and the full traceback were not kept.
Returning no rows for a failed read was a gap of its own all the same: a
Coordinator being replaced and a broken query both read as "no incident
yet", so a wait could not tell an outage from an answer.
Whatever the exact path, the harness did not expect the Coordinator to be
unreadable in S5, where it is unreadable twice by design (while its
container restarts and while its Pod is replaced), nor in S6, and treated a
read that failed for a few seconds like one that can never succeed.

What changed (offline, `experiments/distributed/kind_acceptance.py` and
`kind_acceptance_live.py`):

- **A failed kubectl call is classified, not swallowed**
  (`transient_failure`). It is transient only when kubectl itself failed
  (exit 1, with no `command terminated with exit code`) and its stderr says
  the Pod was not found, the container was not running (not found, waiting
  to start, exited, no host assigned), or the connection was refused, reset,
  lost or timed out (the API server or the kubelet); or when a command in
  the container was ended by SIGKILL or SIGTERM with its container (137,
  143). A kubectl that does not answer within its own timeout is transient
  too. Everything else, a query that ran and failed, a denial or an
  unclassified kubectl error included, raises at once and is never asked
  again. A transient failure raises `TransientRead`, whose reason is the
  class, never kubectl's text. A Deployment with no running Pod (being
  replaced) is transient as well (`Lab.pod`). Failure messages now name
  the tool and its verb (`kubectl exec failed (exit 1): container not
  running`), still never an argument, which may be a Secret's name.
- **A failed read is never "no rows".** `Lab.query` raises `TransientRead`
  or `AcceptanceError`; it no longer returns `[]` on failure.
- **Reads are asked again with bounded back-off** (`Lab.wait`,
  `Lab.read`). A wait asks a transiently failing check again, the pause
  doubling from its interval up to `READ_BACKOFF_SECONDS` (8 s), never past
  the step's own deadline. When the deadline comes while the reads are
  still failing, the wait ends in `ReadUnavailable`, a harness error naming
  the read, its attempts and its last class, not in "not reached in time",
  which would be the system's answer. A single read outside a wait (a
  strict query before a run, a database copy, a Pod's name for its logs,
  an S9 probe's exec, the proposal read before S6's approval and S8's
  revocation) is asked again for up to `READ_RETRY_SECONDS` (180 s, a
  replaced Pod's Ready time). Whether a fault point was reached is read in
  `first_reached`'s wait, bounded by that step's timeout; arming,
  disarming and releasing one are not asked again. Only the wait's own
  expiry, before a single attempt ran, makes `Lab.read` a
  `ReadUnavailable`: any other error from the attempt is raised as it is,
  so an approval or revocation POST that timed out after it was sent ends
  the run as `TimeoutError`, not as a read that never happened. Each run of
  failed reads is kept in the summary's `read_outages` (what, since,
  attempts, last class, seconds, whether it recovered), also when the Pod
  answered and the attempt then failed some other way. `Lab.settle` reads
  once per pass, as before, and a failed read keeps the lab unsettled.
- **The operator thread outlives every failure** (`_operator`). Each failed
  attempt is caught, logged by class (`dist_acceptance.operator.failed`,
  class only) and tried again on the next tick. The summary's `operator`
  holds the count, the first 20 failures (time, kind, class), each outage
  with its length and how it ended, and whether the thread was still in a
  call when the run ended. A transient outage longer than
  `READ_RETRY_SECONDS`, a query error or any unexpected error is fatal: it
  is added once to `harness_error`, so the run fails, and the thread keeps
  going. A refusal from the approval API is recorded and the action asked
  again.
- **S5 reads through the outages and measures Ready from the cluster.**
  It no longer runs `kubectl rollout status` or reads the Pod by name
  straight after the delete: it waits, through failed reads, for the one
  Running, Ready Coordinator Pod, then for one whose UID is not the deleted
  Pod's. `notes.pod_delete.ready_at`, the RTO's "to Ready", is that
  replacement's Ready `lastTransitionTime` (the cluster's clock), and the
  scenario returns only once the replacement's database answers
  (`readable_at`, the harness's clock). "To first claim" and "to next
  transition" come from the copied records, which are copied through the
  same bounded reads. `notes.after_commit_exit` also records the
  container's restart count.
- **S6, S8 and S9 were checked for the same pattern.** S6 partitions the
  Coordinator from the workers only; the harness reads it through kubectl
  exec and approves over the port-forward. That the partition cuts neither
  is an assumption still to confirm live; if it holds, S6 reads normally,
  and a failed read is ridden out like any other. Only the reads before
  S6's approval and S8's revocation are asked again, never the POST
  itself, so neither is sent twice (the operator thread, as before, may
  approve again after a failed POST). (Later: S6 r1 at 0b5529f showed
  the partition can cut the port-forward, and a POST that fails in transit
  is now reconciled from the store before it is ever sent again, in S6, S8
  and the operator thread alike; see
  [S6 r1](#s6-r1-0b5529f-an-approval-post-cut-in-transit).) S8's
  delay applies to the holder's traffic to the API server, not to the
  harness's; its seal wait reads through `Lab.wait`. S9 runs each probe
  again when kubectl could not start it (the Pod restarting), and
  classifies a probe that ran as it answered.

To confirm live, not assumed: that kubectl 1.37's stderr for a Pod being
replaced matches these classes. An unclassified failure stops the run with
a harness error at once (it does not pass), naming only the verb and exit
status, so a new class needs adding by hand. If the harness's own process
is the one that cannot reach the API server for longer than a step's
limit, every read fails and the run stops with `ReadUnavailable`. A
summary is still not written when the records cannot be copied at all
(`collect` raises `ReadUnavailable` after 180 s); the matrix then stops,
with the cleanup and settle already done.

Tests: `tests/test_kind_outage_reads.py`, with the real `Shell` and `Lab`
over a fake kubectl. The classification of each kubectl failure; a wait,
a strict read, a database copy and a fault point that fail N times and then
answer go on, with the pauses backing off and the outage recorded, and ones
that never answer end in `ReadUnavailable` within the step's deadline; a
query error or a denial is raised after one attempt; the operator survives
N failed reads (each transient class) and then approves once, a transient
outage past the bound is fatal once while the thread keeps going, a query
error or an unexpected error is fatal and a refusal is not; an approval
or revocation POST that times out in S6 or S8 is a `TimeoutError`, sent
once, not an unread proposal (since [S6 r1](#s6-r1-0b5529f-an-approval-post-cut-in-transit):
in doubt, reconciled and sent again within its bound, then
`ApprovalInDoubt`, still not an unread proposal); a real thread
stays alive through failures; `run_one` puts the operator's record in the
summary, fails the run on a fatal one and records a terminal wait that
ended in `ReadUnavailable`; and S5 against a scripted Coordinator that
exits, restarts, is deleted and replaced (with one refused Pod list in
between) measures Ready from the replacement, and stops with
`ReadUnavailable` when the replacement never answers. At b37017e every one
of these tests fails. They import the harness by name, so all of them use
one module; none of them would have seen the failure below.

### S5 r1 refused at its first poll: one module run twice

On 14 Nov 2026 the matrix was started again on `kind-sentinel-dist` with
the changes above (`python -m experiments.distributed.kind_acceptance run`
with `--scenarios S5,...`). In S5 r1 the Coordinator exited at its fault
point, as the scenario intends. On the very next poll in
`Lab.first_reached`, at 07:29:46Z, the harness printed `Refused: kubectl
exec failed (exit 1): container not running` and exited 2. No S5 summary
was written and the target was not restored: `run_one` never reached
`Lab.restore` or `Lab.settle`. The heavy-lab lock is released by `main`'s
`with` on any exception. What `Run.cleanup` did in `run_one`'s `finally`
is not known from the output.

The failure was classified correctly: "container not running" is a
`TransientRead`, which `Lab.first_reached` and `Lab.wait` exist to ask
again. They did not, because the harness was two modules. Run with `-m`,
the file is the module `__main__`. `main()` built its `Shell` from
`__main__` and then imported `kind_acceptance_live`, which imports
`experiments.distributed.kind_acceptance` by name: a second module object,
with its own `Shell`, `TransientRead`, `ReadUnavailable` and
`AcceptanceError`. `Lab` comes from that second module. So `Shell.run`
raised `__main__.TransientRead`, and `Lab.first_reached`, `Lab.wait`,
`Lab.read`, `run_one`'s `except`, `Run.cleanup`'s failures and every
`suppress(AcceptanceError, ...)`, which name the imported module's classes,
matched none of it. Only `main`'s own `except AcceptanceError`, in
`__main__`, caught it, and refused. The offline tests import the module by
name, so in them every class is one class. This is very likely also what
stopped the [b37017e S5 run](#s2-s9-matrix-b37017e-reads-while-the-coordinator-pod-was-replaced).

What changed. The `__main__` block of `kind_acceptance.py` imports
`experiments.distributed.kind_acceptance` and runs that module's `main`,
so no class defined in `__main__` is used; the command line is unchanged.
`experiments/boutique/harness/live.py`, which `connected.py`,
`chaos_recovery.py` and `multi_fault.py` import by name, does the same.
It was not seen to fail: its documented entry point,
`scripts/test_boutique_live.py`, already runs the imported module, and run
with `-m` it shares no exception class or state that way. Every other
module with a `__main__` block was checked: none of the modules it imports
imports it again by name, except `agent/datasets/evaluate.py`, from which
`baselines.py` imports two functions that raise only `ValueError`, and
whose documented entry point is `scripts/evaluate_rcaeval.py`.

Tests: `tests/test_main_module_identity.py` starts the documented command
line in a child process (`python -m experiments.distributed.kind_acceptance
run --context kind-sentinel-dist --scenarios S5`), with a fake kubectl and
a fake `live.run` installed by a `sitecustomize`
(`tests/support/kind_cli_hook.py`), so no command reaches a cluster and the
heavy-lab lock is taken in the child's own temporary folder. The fake run
builds the `Lab` as `live.run` does and polls a fault point with
`Lab.first_reached`; the first exec finds the container not running. The
poll is asked again, the run goes on and the outage is recorded once, with
the `Shell` the CLI built being the imported module's; at bc4dc70 the same
child prints the live refusal word for word and exits 2. An
`AcceptanceError` of the imported module is refused by `main` with exit 2;
at bc4dc70 it is a traceback and exit 1. A sweep over every module with a
`__main__` block requires each one that a module it imports imports again
by name to run its imported module, with `agent/datasets/evaluate.py`
listed as safe and why; at bc4dc70 it fails for `kind_acceptance` and the
Boutique harness. Not yet re-run live.

### S6 r1 (0b5529f): an approval POST cut in transit

On 14 Nov 2026 deploy 13, at 0b5529f, ran S6 three times on
`kind-sentinel-dist` (run directory `sentinel-dist-20261114T161109`). S6 r2
and r3 passed. S6 r1 failed. Its NetworkChaos `s6-r1-all`, which cuts the
Coordinator off from every worker, was created at 08:21:42.9Z and
confirmed `Injected`. S6 then sent its own approval through the
port-forward to the Coordinator's approval port, and the POST failed with
`SSLEOFError: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation
of protocol`. The Coordinator's `approval_http` access log has no `create`
between 08:21:40.6 and 08:21:48.6, so the request never reached it. S6
raised. The run's cleanup removed the partition at 08:21:44.9, 2 s into a
partition meant to last three leases. The operator thread, started once S6
returned, approved the action at 08:21:48.6, and the incident ended
RECOVERED. The run failed, as it should: its `harness_error` was the
`SSLEOFError`, and its expectation failed with "template writes during the
partition not recorded as 0", because S6 never got as far as watching the
partition.

What this shows. The [b37017e changes](#s2-s9-matrix-b37017e-reads-while-the-coordinator-pod-was-replaced)
assumed, without checking, that the S6 partition cuts neither the
harness's kubectl exec nor its port-forward. The port-forward assumption
is wrong at least some of the time: in r1 the partition cut the approval
POST, while in r2 and r3 the POST got through. Why it cut that one
connection, and only in r1, is not known. The exec reads were not seen to
fail. Nothing
unsafe happened: no write was made during the partition, and the run did
not pass.

What changed (offline, `experiments/distributed/kind_acceptance.py` and
`kind_acceptance_live.py`):

- **A POST that fails in transit is in doubt** (`Lab._send`). For an
  approval or a revocation, an SSL EOF, a connection reset, refused or
  aborted, a socket timeout or a vanished port-forward (`IN_TRANSIT`:
  `OSError` and `http.client.HTTPException`) means the harness does not
  know whether the request reached the Coordinator. So does a reply cut
  after its headers: `ApprovalClient.post` reads a short body without an
  error (and a ragged TLS EOF is suppressed), then raises its own
  `ProtocolError` 502 because the body is not JSON. The approval API never
  answers 502 (it answers 400, 401, 403, 404, 408, 409, 413 and 503, each
  with a JSON body), so a 502 (`CLIENT_MADE_STATUS`) is in doubt too, with
  error class `ProtocolError502`. The POST is kept in `Lab.in_doubt` from
  the moment it fails until the store shows it recorded or a send of it
  is answered. Before anything else is sent, the harness reads the Coordinator's store
  through kubectl exec (read-only, asked again with back-off for up to
  `APPROVAL_RECONCILE_SECONDS`, 30 s). For an approval it looks for the
  managed approval in `approvals` and `managed_approvals` matching
  everything the request set (operator, proposal digest, policy version and
  the expiry the harness chose); for a revocation, the record in
  `approval_revocations` for that approval, with the operator and the
  reason. If the record is there, the POST is done and recorded as
  reconciled. If it is not there, the same body is sent again, an exact
  retry the Coordinator answers idempotently, over a new port-forward (the
  old one is stopped first, whether or not it is still running). Each
  failure is reconciled again before the next send. The harness sends at
  most `APPROVAL_SEND_ATTEMPTS` (3) times, and sends nothing more once
  `APPROVAL_RESEND_SECONDS` (60 s) have passed since the first send. Past
  that, or when the store cannot be read to tell (whatever the failure:
  a read that kept failing, kubectl that could not be started, a record
  that does not parse), the result is `ApprovalInDoubt`, a harness error
  that fails the run and is never asked again by `Lab.read`; the POST stays
  in `Lab.in_doubt`. An HTTP error answer (a 4xx or 5xx other than the
  client's own 502, `ProtocolError`) is the approval API's own answer, not
  in doubt: it is raised as it is and never sent again. A resend whose
  port-forward or client cannot be set up is recorded as `not_sent` and
  raised as it is; the earlier POST stays in doubt.
- **No double approval.** The Coordinator accepts an approval only while
  the action waits for one, or as an exact retry of the one it holds, and
  records it in the same transaction that moves the action on
  (`services/coordinator/core.py`, `approve`). A send that lands after its
  reconciliation read it as not recorded therefore turns any later
  approval into a 409, never a second record. A POST still in doubt is
  kept in `Lab.in_doubt`; the next `approve` or `revoke` for that action
  reads the store for it first. If the store shows it not recorded, that
  same body is sent again over a new port-forward, so a late landing of it
  reads as the idempotent answer to an exact retry rather than a 409. An
  approval is sent again unchanged only while `APPROVAL_REUSE_SECONDS`
  (120 s) of its 240 s lifetime are left; after that a new one is sent.
- **The operator thread** (`approve_pending`) goes through the same path.
  Before, a POST that failed in transit counted there as a transient
  failure, and the next tick sent a new approval without reading whether
  the first had landed. Now the POST is reconciled inside `Lab.approve`.
  One still in doubt past the bound is fatal for the run, and the next tick
  reads the store before it sends anything.
- **Every attempt is recorded.** Each POST and each reconciliation (send
  time, outcome `answered`, `refused`, `not_sent` or `in_doubt`, error
  class (and, for an unreadable store, the reconciliation's), whether a
  new port-forward was used, `recorded`, `not_recorded` or `unreadable`)
  is in `Lab.approval_attempts`. S6's and S8's appear in their
  `notes.approval_attempts`, kept also when the scenario raises, and
  every attempt of the run, the operator thread's included, appears in the
  summary's `approval_attempts`. `operator_actions` says whether each
  approval was confirmed by the answer or by the store.
- **S6's timing.** The three-lease watch starts once the approval is known
  to be recorded and must fit in what is left of the partition, counted
  from its creation (`Run.window_left`). Approval attempts use up part of
  that time, and a watch they cut short fails the scenario with "the
  partition had only N s left". `notes.during_partition` keeps the template
  writes counted from the fault time, the seconds watched, and
  `approval_recorded_after_cut_seconds` (from the partition's creation, as
  `Run.window_left` counts, through its confirmation, to the approval being
  known recorded). The partition is long enough for three leases after a
  confirmation that takes all of `CHAOS_CONFIRM_SECONDS` only when the
  approval is quick: in the worst case three sends with a 10 s timeout and
  three reconciliations of up to 30 s each take more than the 60 s spare,
  and the watch is then cut short and S6 fails. The partition stays in place while
  the approval is in doubt. If the POST was recorded before its answer was
  cut, the watch starts later than the approval landed. The window is then
  longer, never shorter, and writes are still counted from the fault time.

Tests: `tests/test_kind_approval_in_doubt.py` runs the real `Lab` against a
real Coordinator store built from the sample messages and read through a
fake kubectl exec running the harness's own read-only query helper. A fake
approval client sits in front of the real `ApprovalAccess` and fails in
transit before or after the request arrives. Covered: an EOF before arrival
is reconciled as not recorded and the same body is sent once more over a
new port-forward (one approval stored); a reset connection is handled the
same way; an EOF or a timeout after the approval was recorded is not sent
again; reconciliation reads through a Pod that fails a few reads; a 409 or
403 answer is raised at once with no reconciliation and no resend; a POST
that keeps failing is `ApprovalInDoubt` after three sends, each reconciled,
and after one send when that send used up the time bound; a store that
cannot be read leaves the POST in doubt with nothing resent, and the next
`approve` reconciles it as recorded; the same for revocations; the
operator thread never approves twice after a lost answer, resends only
what was not recorded, and is fatal when the bound is passed; and S6
reconciles an EOF, resends once, watches three full leases with no write
and records its attempts and timing, fails with its attempts in the notes
when the approval never lands, and still reports a write made during the
partition after a reconciled approval. At 0b5529f every one of these tests
fails.

A review of that change found three gaps, now closed. A reply cut after its
headers became the client's own `ProtocolError` 502 and was recorded as
`refused` and raised with no reconciliation, although the Coordinator
records an approval before it answers. A reconciliation that failed with
anything but an `AcceptanceError` (kubectl that could not be started, a
record that did not parse) left the POST out of `Lab.in_doubt`, so the
operator's next tick would have sent a new approval blind. And
`approval_recorded_after_cut_seconds` was counted from the partition's
confirmation, not from its creation. Tests added: a reply cut after its
headers is reconciled as recorded and not sent again (for an approval, a
revocation, the operator thread and S6), and a 502 for a POST that never
landed is reconciled as not recorded and sent once more; the real
`ApprovalClient`, against a loopback endpoint that records the request
and cuts its reply after the headers (no body or half a body), raises its
own 502, and `Lab.approve` through it reconciles that as recorded; a
reconciliation that fails with `OSError` is `ApprovalInDoubt` with the POST
kept in doubt, and the next `approve` (and the operator's next tick)
reconciles it and sends the same body again; a resend whose port-forward
cannot start is recorded as `not_sent` and the POST stays in doubt; an
approval in doubt with less than two minutes left is replaced by a new
one; and S6 counts its approval time from the partition's creation. At
888180f, the first version of this change, these tests and the two
changed S6 tests of `tests/test_kind_chaos_confirmation.py` give 6
failures and 9 errors. Not yet re-run live. The `tests/test_kind_outage_reads.py` tests that a POST timing out
in S6 or S8 was a `TimeoutError` sent once now expect it in doubt instead:
reconciled, sent again within the bound, then `ApprovalInDoubt`. Not yet
re-run live.

### S7 r4 refused at its restore: the target's resourceVersion moved

On 14 Nov 2026 the matrix ran on `kind-sentinel-dist` from deploy 13 at
0b5529f (run directory `sentinel-dist-20261114T161109`). S5 passed 5 of 5
and S6 4 of 5. S7 r1, r2 and r3 passed: each ended FAILED, as S7 expects,
with no template write, under the 300 s TimeChaos skew on both Executors.
During S7 r4 the harness printed `Refused: kubectl patch failed (exit 1)`
and exited 2. No S7 r4 summary was written, the matrix stopped there, and
`demo-api` was left faulted (`STARTUP_FAILURE=true`, generation 29, 0 of 1
Pods ready).

What is known and what is not. The only `kubectl patch` the harness sends
in a run is `Lab.restore`'s, which S7 needs because its incident ends
FAILED and Sentinel puts nothing back. That patch is one JSON patch: a test
of the baseline's uid, a test of the resourceVersion `Lab.restore` had
just read, and the template. kubectl's stderr was not kept, and the message
named only the verb and the exit status, so why the API server refused it
is not known. The most likely cause is that the resourceVersion test
failed: the target was crash-looping, so the Deployment controller kept
writing its status, and each status write moves the resourceVersion
between the harness's read and its patch. This is the same race the
Executors' CONFLICTs showed in this lab
([third deployment](#third-deployment-recreate-and-conflicts)). It is not
proved. And the refusal escaped `run_one`: the restore ran in its
`finally`, before the summary, so a failed restore meant no summary at all.

What changed (offline, `experiments/distributed/kind_acceptance.py` and
`kind_acceptance_live.py`):

- **A refused patch or apply names its class** (`write_failure`,
  `WriteRefused`). A non-zero exit status makes the harness classify
  kubectl's stderr; the class comes from stderr alone: `test failed` (the
  API server's 422 for a JSON patch test, in either the generic or the
  json-patch wording; the generic wording can also cover other JSON patch
  errors), `conflict`, `admission denied` (matched before `not found`, as a
  policy's message may contain those words), `not found`, `forbidden`,
  `invalid`, or `stderr not classified`. The message
  is `kubectl patch failed (exit 1): test failed`. It never includes
  kubectl's text, which names the object and can quote the patch. A
  transient failure is still `TransientRead`, and a failed read's message
  is unchanged.
- **The restore reads the target again and retries, keeping both tests**
  (`Lab.restore`). After a `test failed` or `conflict` refusal, or a
  transient failure, it reads the Deployment again and acts on what it
  finds. If the uid has changed, it refuses. If the template is already
  the baseline, it does nothing (`operator_restore` false, with a note;
  null if its own earlier patch failed transiently and may have applied).
  If the template is the one the restore first read, it patches again with
  the new resourceVersion. Any other template is refused, never
  overwritten. Every patch tests the uid and the resourceVersion read just
  before it; no patch drops a test or uses a merge patch. The restore makes
  at most `RESTORE_ATTEMPTS` (5) attempts within `RESTORE_RETRY_SECONDS`
  (30 s), pausing 2 s between them, and then stops with an error saying
  the target may still be faulted. The time bound is checked between
  attempts, so a slow kubectl call can take it past 30 s; the count bound
  is exact. Any other refusal raises at once. The result lists each
  attempt's resourceVersion and outcome, and a refusal or exhaustion
  carries the attempts so far. The "faulted" template is the one the
  restore reads first; it is not compared with the injected
  `STARTUP_FAILURE=true` template, so a template another writer put there
  before that read is still overwritten, under both tests, as before.
- **A failed restore or settle is in the run's summary** (`run_one`). The
  error is recorded as `restore_error`, prefixed `restore:` or `settle:`,
  and added to `harness_error`, so the run fails and never passes. The
  settle is skipped when the restore failed. The cleanup, the record copy
  and the evidence go on as before. `run` then stops the matrix, after
  writing the summary and the overview, with a message that the lab needs
  `demo-api` restored by hand or a teardown before the next run. The
  restore's attempts are in the summary's `operator_actions` (`restore
  failed`). When the wait for a terminal state, or the generation read
  after it, raises something other than `ReadUnavailable`, no summary can
  be written; the restore failure is then raised with that error, as one
  `AcceptanceError` naming both (chained from the wait's error) and the
  manual restore, rather than lost under it. An interrupt keeps escaping
  as it is, with the restore failure as a note.

Tests: `tests/test_kind_restore_race.py` uses the real `Shell` and
`Lab.restore` over a fake kubectl. The fake holds one Deployment and
checks a JSON patch's tests the way the API server does. Covered: a
resourceVersion that moves once (each refusal wording, and a conflict) is
read again and patched once more, and every patch carries both tests; one
that keeps moving stops at the bound with a clear error and the template
left faulted; a changed uid and a template written by someone else are
refused and not overwritten; a target that another writer already restored
is left alone; a forbidden, denied or not-found patch is raised after one
attempt with its class; transient failures are retried within the same
bound, and a lost answer is reported in doubt; and patch and apply messages
carry a class but never an argument or the patch. Through `run_one` and
`run`: a restore that keeps losing the race, or raises, and a settle that
raises each write a failed summary with the error, the cleanup still runs,
and the matrix stops after that run with the manual-restore message; a
wait or generation read that raises while the restore also fails raises
the restore failure with the manual-restore message, chained from the
wait's error, and a wait that raises over a good restore raises as before.
At 0b5529f 16 of these 17 tests fail; the one that passes is that last
regression check. Not yet re-run live; the S7 r4 target
must be restored by hand or the lab torn down before the next run.

## Results

Generated by `python -m experiments.distributed.kind_acceptance table`;
replace the table with the one a live run prints.

| Scenario | Runs | Runs passed | Invariants passed | Expectation met | Max template writes per action (audit) | Takeover latency (s) | Coordinator RTO to Ready / first claim (s) |
|---|---|---|---|---|---|---|---|
| S0 | not yet run | - | - | - | - | - | - |
| S1a | not yet run | - | - | - | - | - | - |
| S1b | not yet run | - | - | - | - | - | - |
| S1c | not yet run | - | - | - | - | - | - |
| S2 | not yet run | - | - | - | - | - | - |
| S3 | not yet run | - | - | - | - | - | - |
| S4 | not yet run | - | - | - | - | - | - |
| S5 | not yet run | - | - | - | - | - | - |
| S6 | not yet run | - | - | - | - | - | - |
| S7 | not yet run | - | - | - | - | - | - |
| S8 | not yet run | - | - | - | - | - | - |
| S9 | not yet run | - | - | - | - | - | - |

The owners review the evidence for #59, #61 and #63 before any of it is
quoted. P4.02 adds the measured RTO and RPO table to the
[distributed model](distributed-model.md).

## Offline tests

- `tests/test_deploy_manifests.py` (`DistManifestTests`): every worker has its
  own Secret and PVC and none mounts the Coordinator's data; the Planner has
  no token, binding or kubeconfig; the Executor may only get and patch
  `demo-api`, and the readers only read; no cluster-wide grant; the
  Coordinator and Executors are pinned to c, a and b; the network policy
  allows exactly the edges in the table above; the kind configuration pins
  the node image by digest and mounts the audit policy. Each check also runs
  on a mutated manifest and must report it.
- `tests/test_kind_acceptance.py`: `kind-sentinel-lab` and every context
  other than `kind-sentinel-dist` refused before any command; each preflight
  refusal (no cluster, a context pointing elsewhere, wrong nodes or labels, an
  unpinned node image, no audit log, another Chaos Mesh chart, too little
  memory, the shared lock held, P4.03 experiments present); rendering, chaos
  object bounds, the scenario matrix, takeover and RTO arithmetic, the
  results table, and evidence refused when it names a Secret or holds a
  credential; each scenario's expectation failing on records the write
  invariants pass; an empty, partial or rotated audit extract failing a
  run; the rotated audit files read with the live one; the clock offset and
  the window moved onto the cluster's clock; `run` refusing a target with
  another UID; and `teardown` deleting nothing through a context that names
  another cluster.
- `tests/test_kind_acceptance_repeats.py`: repeated runs in one deployment,
  with the real `Lab` waiting, approving, settling and scoping over local
  Coordinator and Observer databases that a scripted Observer and
  Coordinator change as virtual time passes. The 13 Nov 2026 symptom: with an earlier RECOVERED
  incident (delivered by the Observer) in the store, S0 waits for its own
  incident and recovers it, and X3 holds; at 2ef594a the same test gives no
  incident. Also: a run with no incident of its own times out instead of
  passing on the earlier one; waiting for a state (S3, S4, S5) ignores
  earlier incidents; an incident opened by the rollback's own rollout ends
  inside its run, and the next run counts neither; an incident escalated
  before the restore is decided by the operator while settling, and the
  approval is recorded; a run that leaves an incident waiting does not pass
  and the next run is refused; an incident the Observer has not delivered
  holds the settle and refuses the next run until it is delivered and ends;
  each condition `run_unsettled` refuses (earlier incident open or
  undelivered, seal required, target off its baseline or not ready, baseline
  too old for the run), and an unreadable Coordinator or Observer store
  refuses rather than reading as empty; X3 still reports an incident no one
  recorded; S8's seal must be newer than earlier runs'; and the Observer
  opens a new incident for a new fault after an earlier one recovered (with
  or without a healthy sample between), but none for a fault put back
  within its failure window, as runs 2-5 were.
- `tests/test_stale_replan.py`: the live symptom over loopback HTTP, with the
  real Planner, Guard and Executor workers and a fake API server whose
  resourceVersion moves after planning (before the Guard's first pass, and
  between the approval and the re-check). With the lab's `--stale-replans`
  the incident is re-planned on the Guard's read, approved again and
  repaired with one write; with the default the stale refusal stays final.
  Also: re-plans are bounded, only a refusal for staleness alone and only a
  ROLLBACK is re-planned, and a malformed re-plan request is refused. With
  the rule disabled, the three symptom tests fail. A change to the spec
  (template and generation) between planning and the Guard, or between the
  approval and the re-check, or after a re-plan, leaves the refusal final
  with no write; so do the S8 shape (approve, ALLOW, revoke, version moves,
  re-check), a revocation before the re-check, a stale re-check after an
  ALLOW, a seal, and a refusal without the Guard's record of the generation.
  The Guard's record is SKIP and never a reason code. The local Planner
  pipeline refuses a re-plan request, and an agent-mode re-plan says in its
  rationale that it is one. The tests of these conditions fail against the
  first version of the rule, which re-planned every stale refusal.
- `tests/test_conflict_replan.py` and `tests/test_verifier_rollout.py`: the
  third deployment's CONFLICTs (r2, r3) and its Pod-less first reading (r1),
  as listed under
  [third deployment](#third-deployment-recreate-and-conflicts).
- `tests/test_kind_chaos_confirmation.py`: every fault confirmed in effect
  before a scenario goes on, a fault that never takes effect a harness
  error, and nothing left behind, as listed under
  [first matrix](#first-matrix-1adcfe3-a-chaos-object-removed-before-chaos-mesh-acted).
- `tests/test_kind_outage_reads.py`: reads that fail while a Pod restarts
  or is replaced are asked again within their step's limit and are a
  harness error past it, in the main thread, the operator thread and S5, as
  listed under
  [S2-S9 matrix](#s2-s9-matrix-b37017e-reads-while-the-coordinator-pod-was-replaced).
- `tests/test_main_module_identity.py`: the documented `python -m` command
  line, in a child process with a fake kubectl, uses one module, so a
  transient read from the `Shell` it built is asked again; and every entry
  point imported again by name runs its imported module, as listed under
  [S5 r1 refused](#s5-r1-refused-at-its-first-poll-one-module-run-twice).
- `tests/test_kind_approval_in_doubt.py`: an approval or revocation POST
  that fails in transit is reconciled from the Coordinator's store and
  sent again only when not recorded, within its bound, in S6, S8 and the
  operator thread, as listed under
  [S6 r1](#s6-r1-0b5529f-an-approval-post-cut-in-transit).
- `tests/test_kind_restore_race.py`: the operator's restore reads the
  target again and retries its guarded patch while the resourceVersion
  moves, refuses a changed uid or a template someone else wrote, and a
  restore or settle that fails is a harness error in the written summary
  that stops the matrix, as listed under
  [S7 r4 refused](#s7-r4-refused-at-its-restore-the-targets-resourceversion-moved).
- `tests/test_kind_acceptance.py` also checks that S6 starts its operator
  only after its runner returns, while S0 approves from the start, and that
  `deploy` applies Sentinel without the workload and stops when the target's
  template moved off its baseline (`apply_sentinel`).
- `tests/test_kind_acceptance.py` also checks that the restore patch goes in
  a file kubectl can open (never `-`), removed afterwards, and that `deploy`
  applies the overlay without the workload.
- `tests/test_distributed_audit.py`: with the fixtures in
  `tests/fixtures/distributed_audit/` (written for the tests, not captured):
  one template write passes I7, two successful template PATCHes for one action
  fail it (and I1), a seal PATCH is a fence and not a write, denied, dry-run,
  status and unfinished requests are not writes, a Sentinel change logged
  without its body fails I7 and I1 while the operator's is reported apart,
  the completeness checks, and the extract drops bodies,
  IPs, agents, credential IDs and Secret names.

## P4.02 drills

```powershell
python -m experiments.distributed.kind_drills rto-from-runs $s5runs --run-dir $run
kubectl --context=kind-sentinel-dist --namespace=sentinel-dist set env deployment/sentinel-coordinator SENTINEL_LOG_LEVEL=DEBUG
kubectl --context=kind-sentinel-dist --namespace=sentinel-dist rollout status deployment/sentinel-coordinator --timeout=180s
python -m experiments.distributed.kind_drills partition --context kind-sentinel-dist --run-dir $run --seconds 120
python -m experiments.distributed.kind_drills restore --context kind-sentinel-dist --run-dir $run
```
