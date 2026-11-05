# Deployment resources

Run deployment commands from the repository root.

| Directory | Contents |
| --- | --- |
| `boutique/` | Pinned upstream Boutique manifest, license and single-service dependency configuration |
| `sentinel/` | Sentinel Kubernetes resources and restricted Executor RBAC |
| `kind/` | Local cluster configuration |
| `docker/` | Sentinel component and packaged-demo Dockerfiles; repository-root build context |
| `workloads/` | Small demo workload Kubernetes manifests |
| `chaos-mesh/` | Chaos Mesh installation integration notes |
| `observability/` | Shared telemetry backend integration notes |

Workload Dockerfiles remain alongside their application sources in `experiments/workloads/`. The Verifier lab uses the repository root as its build context so it can install the shared `requirements.txt`:

```powershell
docker build -f experiments/workloads/verifier-lab/Dockerfile -t sentinel-verifier-lab:0.1.0 .
```

The small `experiments/workloads/demo/` application keeps its own directory as its build context.
