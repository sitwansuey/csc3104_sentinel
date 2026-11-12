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

**Status: written and tested offline only.** The manifests, the cluster
configuration, the audit policy and the harness exist and are checked by
unit tests. No `sentinel-dist` cluster has been created, nothing here has run
against an API server, and the results table below is empty. A later live run
fills it.

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

## Scenarios

`python -m experiments.distributed.kind_acceptance plan` prints this matrix
and a sample of each Chaos Mesh object, offline.

| Scenario | Fault | File-mode counterpart | Expected |
| --- | --- | --- | --- |
| S0 | none: the operator approves through the approval API | S0 | RECOVERED, 1 template write |
| S1a | fault point `planner.mid_inference`, then PodChaos pod-kill of the Planner | S1a | RECOVERED, 1 write |
| S1b | fault point `executor.after_start_write`, then PodChaos pod-failure of that Executor until the incident ends | S1b | RECOVERED, 1 write, takeover measured |
| S1c | fault point `executor.after_patch_before_save`, then pod-failure as in S1b | S1c | RECOVERED, 1 write, takeover measured |
| S2 | fault point `executor.before_patch`, then `docker pause` of the holder's node past its lease | S2 | RECOVERED, 1 write; the stale holder's patch refused |
| S3 | fault point `executor.after_start_write`, then NetworkChaos partition between the holder and the Coordinator (the API server stays reachable) | S3 | terminal, at most 1 write |
| S4 | fault point `executor.after_patch_before_save`, then NetworkChaos partition from the Coordinator to the holder until the result is recorded | S4 | RECOVERED, 1 write, saved result replayed |
| S5 | fault point `coordinator.after_commit_before_reply` (exit), then `kubectl delete pod` of the Coordinator | S5 | RECOVERED, 1 write, Coordinator RTO measured |
| S6 | NetworkChaos partition between the Coordinator and every worker for three leases, approval given meanwhile | S6 | 0 writes during the partition, progress after |
| S7 | TimeChaos -300 s on both Executors | S7 | FAILED with 0 writes |
| S8 | fault point `executor.before_patch`, revoke the approval, then 8 s NetworkChaos delay on the holder's API traffic | S8 | FAILED, 0 writes, 1 seal, the stale patch refused in the audit log |
| S9 | the Executor reads a Secret, patches another Deployment, opens pods/exec and changes `spec.replicas`; the Guard patches the target; the Planner and an Executor open TCP connections they must not | design S9 | every request denied or timed out, the Verifier's allowed control open, 0 writes |

S2 here is the stall of the file-mode catalogue, done with `docker pause` of
the holder's node, which is the A6 design's S3; S3 here is the network
partition of the catalogue. S7 expects what file mode showed: with operator
approval required, an Executor whose clock is 300 s behind refuses the
Coordinator-stamped approval, so the action ends FAILED with no write.

Measured in each run: takeover latency (S1b, S1c, S2, S3: from the fault to
the other Executor's first claim, from the Coordinator's access log) and, in
S5, the Coordinator's recovery after the Pod deletion (to Pod Ready, to its
first answered claim and to the next journal transition).

## Live recipe

Run from the repository root in PowerShell, with the project virtual
environment active, Docker Desktop running Linux containers, and kind
v0.33.0, kubectl, Helm and openssl on the path. Do not run it while the P4.03
Chaos Mesh lab is running: `preflight` refuses while the shared lock
`sentinel-heavy-lab.lock` in the system temporary directory is held or Chaos
Mesh experiments exist in `kind-sentinel-boutique-connected`. Every command
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

`run` writes, per scenario and repeat, `<date>/<scenario>-r<n>-<time>z/`
holding `summary.json`, `timeline.jsonl`, `audit-extract.jsonl`,
`records.json` and `versions.json`, and at the top `summary.json` and
`results.md` over every run in the directory. The databases and raw logs it
copies out of the Pods to compute the invariants are deleted after each run.
After each run the operator puts the target's baseline template back when
Sentinel did not, under uid and resourceVersion tests, and records it as an
operator write.

Things a first live run must confirm rather than assume: that the node image
digest in `kind-dist.yaml` is the one the kind v0.33.0 release notes list for
`kindest/node:v1.37.0` (it was written down offline and has never been
pulled; a wrong digest makes `kind create cluster` fail rather than run
another image), that kindnet
enforces the network policy (S9's TCP probes) while still letting the kubelet
probe the Coordinator's `/readyz` over its Pod address, that Chaos Mesh's pod-failure
pause image can be pulled into the cluster, that TimeChaos shifts the clock
Python reads in the worker image, that `kubectl port-forward` to the
Coordinator still works during the S6 partition, and that the audit log
carries the Pod name of each ServiceAccount request.

## Results

Generated by `python -m experiments.distributed.kind_acceptance table`;
replace the table with the one a live run prints.

| Scenario | Runs | Invariants passed | Max template writes per action (audit) | Takeover latency (s) | Coordinator RTO to Ready / first claim (s) |
|---|---|---|---|---|---|
| S0 | not yet run | - | - | - | - |
| S1a | not yet run | - | - | - | - |
| S1b | not yet run | - | - | - | - |
| S1c | not yet run | - | - | - | - |
| S2 | not yet run | - | - | - | - |
| S3 | not yet run | - | - | - | - |
| S4 | not yet run | - | - | - | - |
| S5 | not yet run | - | - | - | - |
| S6 | not yet run | - | - | - | - |
| S7 | not yet run | - | - | - | - |
| S8 | not yet run | - | - | - | - |
| S9 | not yet run | - | - | - | - |

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
  credential.
- `tests/test_distributed_audit.py`: with the fixtures in
  `tests/fixtures/distributed_audit/` (written for the tests, not captured):
  one template write passes I7, two successful template PATCHes for one action
  fail it (and I1), a seal PATCH is a fence and not a write, denied, dry-run,
  status and unfinished requests are not writes, and the extract drops bodies,
  IPs, agents, credential IDs and Secret names.
