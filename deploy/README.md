# Deployment resources

Run deployment commands from the repository root.

The Coordinator worker API has its own [ordered deployment and validation
guide](sentinel/COORDINATOR.md), image, TLS Service and persistent volume. This
is the Coordinator portion of #62. The [remote worker entrypoint](../docs/remote-workers.md)
binds component algorithms and owned journals, and `kubectl apply -k deploy/sentinel`
selects the HTTPS Observer composition. `docker/worker.Dockerfile` packages every
worker role in one image ([worker image](../docs/remote-workers.md#worker-image));
it has been built and scanned locally, but no container has run from it. No worker rollout or whole-platform
Kubernetes acceptance has been run: the networked layer has only run on one
host (`L-SINGLE-HOST-DISTRIBUTION` in [limitations](../docs/limitations.md)).
`sentinel/sentinel-dist/` and `kind/kind-dist.yaml` are the complete rollout for
the four-node P4.01 lab, written and tested offline only
([distributed acceptance](../docs/distributed-acceptance.md)).

| Directory | Contents |
| --- | --- |
| `boutique/` | Pinned upstream Boutique manifest, license and single-service dependency configuration |
| `sentinel/` | Sentinel Kubernetes resources, restricted Executor RBAC and the Executor admission policy; `sentinel/sentinel-dist/` holds every role for the four-node lab |
| `kind/` | Local cluster configuration, including the separate Calico security lab and the four-node `sentinel-dist` lab with its API server audit policy |
| `docker/` | Sentinel component, worker, security inspector and packaged-demo Dockerfiles; repository-root build context |
| `workloads/` | Small demo workload Kubernetes manifests |
| `chaos-mesh/` | Pinned lab installation values; see `docs/chaos.md` |

Workload Dockerfiles remain alongside their application sources in `experiments/workloads/`. The Verifier lab uses the repository root as its build context and installs its own SDK requirements:

```powershell
docker build -f experiments/workloads/verifier-lab/Dockerfile -t sentinel-verifier-lab:0.1.0 .
```

The small `experiments/workloads/demo/` and `experiments/workloads/otel/` applications keep their own directories as build contexts. The security candidate Dockerfile and shipping protocol live in `experiments/workloads/security/`; `scripts/security/build_security_candidate.py` prepares its upstream build context.

## Executor admission policy

RBAC limits the Executor to get and patch on the Deployments its Role names
(one in `sentinel/executor-rbac.yaml`; the Boutique lab harness generates the
Role from that file for each lab's own namespace and its enabled targets), but
not to fields. `sentinel/executor-admission.yaml` is a ValidatingAdmissionPolicy and
its binding. The policy matches requests from any ServiceAccount named
`sentinel-executor`, in any namespace; the binding applies it only to
Deployments in the `sentinel-demo` namespace. It is meant to let that identity
change the Pod template only together with new `sentinel.io/execution`
evidence, change only the `sentinel.io/execution`, `sentinel.io/fence` and
`sentinel.io/fence-epoch` annotations, and never lower the fence epoch.
Replicas, labels, other annotations and the other DeploymentSpec fields that
exist today (8 in all) are denied; a field Kubernetes adds later is not held
until it is added to the policy. It cannot limit what the Pod template
contains. It needs Kubernetes 1.30 or later, and an administrator applies it
after `executor-rbac.yaml`; no launcher installs it:

```powershell
kubectl --context <context> apply -f deploy/sentinel/executor-rbac.yaml
kubectl --context <context> apply -f deploy/sentinel/executor-admission.yaml
```

To remove it, delete the binding first:
`kubectl --context <context> delete validatingadmissionpolicybinding,validatingadmissionpolicy sentinel-executor-writes`.

**The fence epoch and a new Coordinator store.** The `sentinel.io/fence-epoch`
a seal writes comes from one Coordinator store's ownership row, which starts
at 1. The annotation stays on the Deployment: a ROLLBACK replaces only the Pod
template. With the policy installed, a new, reset or restored store (each
live experiment run makes its own) cannot seal a Deployment that carries a
higher epoch until its own epochs pass that value. Every seal patch is denied,
the seal gives up after 5 passes as UNSEALED, and the workload goes to review
and stays blocked. Before pointing a new store at such a Deployment, remove
the annotation, or recreate the Deployment:

```powershell
kubectl --context <context> --namespace sentinel-demo annotate deployment demo-api sentinel.io/fence-epoch-
```

The Executor cannot tell a policy denial from a lost reply, so it reports any
denied write as `WriteOutcomeUnknown` and settles it by reconciliation from a
read. Whether to seed a store's epoch from the annotation, or to drop the epoch
rule and fence by resourceVersion only, is an open owner decision
(`L-EXECUTOR-FIELD-LIMITS` in [limitations](../docs/limitations.md)).

The policy has been checked structurally (`tests/test_deploy_manifests.py`,
which also pins each CEL expression to its reviewed text) but not against an
API server. The live check installs the same policy under a run-specific name,
bound to one new namespace on `kind-sentinel-boutique-connected`, sends every
denied and allowed request with the restricted Executor token, and checks that
each denial names the rule expected to deny it. It includes a seal at epoch 1
after the stored epoch reached 6, which must be denied, so the failure mode
above is seen before anyone relies on the policy. `kubectl scale` is expected
to be denied by RBAC (no scale subresource grant), and a dry-run label patch
checks that the policy's username match catches the Executor token. For
pods/exec it records that `kubectl auth can-i` answers no; it attempts no exec.
It removes the namespace, policy and binding on exit, and refuses any other
context and anything naming `sentinel-lab`. It ran once, on 12 Nov 2026 at
599f44f against Kubernetes 1.37, and matched 24 of 24 probes, including the
epoch-1 seal denied as described above (`C-ADMISSION-POLICY-LIVE`):

```powershell
python scripts/validation/test_executor_admission_live.py --context kind-sentinel-boutique-connected
```

If the harness is killed before it cleans up, everything it made is labelled
`app.kubernetes.io/managed-by=sentinel-admission-harness`:

```powershell
kubectl --context kind-sentinel-boutique-connected delete validatingadmissionpolicybinding,validatingadmissionpolicy,namespace -l app.kubernetes.io/managed-by=sentinel-admission-harness
```

It writes `admission.json` (exit code and server message per check, no token
or server address) under `data/sentinel/runs/executor-admission-<time>/`. A
passing run would be recorded as an evidence bundle under
`evidence/distributed/<date>/admission/`.
