# Deployment resources

Run deployment commands from the repository root.

The Coordinator worker API has its own [ordered deployment and validation
guide](sentinel/COORDINATOR.md), image, TLS Service and persistent volume. This
is the Coordinator portion of #62. The [remote worker entrypoint](../docs/remote-workers.md)
binds component algorithms and owned journals, and `kubectl apply -k deploy/sentinel`
selects the HTTPS Observer composition. No worker rollout or whole-platform
Kubernetes acceptance has been run: the networked layer has only run on one
host (`L-SINGLE-HOST-DISTRIBUTION` in [limitations](../docs/limitations.md)).

| Directory | Contents |
| --- | --- |
| `boutique/` | Pinned upstream Boutique manifest, license and single-service dependency configuration |
| `sentinel/` | Sentinel Kubernetes resources, restricted Executor RBAC and the Executor admission policy |
| `kind/` | Local cluster configuration, including the separate Calico security lab |
| `docker/` | Sentinel component, security inspector and packaged-demo Dockerfiles; repository-root build context |
| `workloads/` | Small demo workload Kubernetes manifests |
| `chaos-mesh/` | Pinned lab installation values; see `docs/chaos.md` |

Workload Dockerfiles remain alongside their application sources in `experiments/workloads/`. The Verifier lab uses the repository root as its build context and installs its own SDK requirements:

```powershell
docker build -f experiments/workloads/verifier-lab/Dockerfile -t sentinel-verifier-lab:0.1.0 .
```

The small `experiments/workloads/demo/` and `experiments/workloads/otel/` applications keep their own directories as build contexts. The security candidate Dockerfile and shipping protocol live in `experiments/workloads/security/`; `scripts/security/build_security_candidate.py` prepares its upstream build context.

## Executor admission policy

RBAC limits the Executor to get and patch on one named Deployment, but not to
fields. `sentinel/executor-admission.yaml` is a ValidatingAdmissionPolicy and
binding for any ServiceAccount named `sentinel-executor` in the `sentinel-demo`
namespace. It lets that identity change the Pod template only together with new
`sentinel.io/execution` evidence, change only the `sentinel.io/execution`,
`sentinel.io/fence` and `sentinel.io/fence-epoch` annotations, and never lower
the fence epoch. Replicas, labels, other annotations and every other spec field
are denied. It cannot limit what the Pod template contains. It needs Kubernetes
1.30 or later, and an administrator applies it after `executor-rbac.yaml`; no
launcher installs it:

```powershell
kubectl --context <context> apply -f deploy/sentinel/executor-rbac.yaml
kubectl --context <context> apply -f deploy/sentinel/executor-admission.yaml
```

To remove it, delete the binding first:
`kubectl --context <context> delete validatingadmissionpolicybinding,validatingadmissionpolicy sentinel-executor-writes`.

The policy has been checked structurally (`tests/test_deploy_manifests.py`) but
not against an API server. The live check installs the same policy under a
run-specific name, bound to one new namespace on
`kind-sentinel-boutique-connected`, sends every denied and allowed request with
the restricted Executor token, and removes the namespace, policy and binding on
exit. It refuses any other context and anything naming `sentinel-lab`. It has
not been run yet:

```powershell
python scripts/validation/test_executor_admission_live.py --context kind-sentinel-boutique-connected
```

It writes `admission.json` (exit code and server message per check, no token
or server address) under `data/sentinel/runs/executor-admission-<time>/`. A
passing run would be recorded as an evidence bundle under
`evidence/distributed/<date>/admission/`.
