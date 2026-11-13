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

**Status: deployed twice, one S0 run passed, no matrix yet.** The
manifests, the cluster configuration, the audit policy and the harness are
checked by unit tests. The `sentinel-dist` cluster was created and deployed
on 13 Nov 2026, and its first S0 runs repaired nothing: the Guard refused
every proposal as stale and the restore after them failed. After those
fixes a second deployment's first S0 run passed, and the five S0 runs after
it in the same deployment each ended in about 15 s with no incident, because
the harness counted the first run's RECOVERED incident as theirs
([first live attempts](#first-live-attempts)). That is fixed in code and not
yet run live. The results table below is empty; a later live run fills it.

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
Coordinator still works during the S6 partition, that the audit log
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
is not re-planned, so S0 can still end without a repair.

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
evidence folder for either; managed fields keep only each manager's latest
write, so who made them is not recorded. In the 21 s between
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
