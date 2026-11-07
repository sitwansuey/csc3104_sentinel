# Deployment resources

Run deployment commands from the repository root.

| Directory | Contents |
| --- | --- |
| `boutique/` | Pinned upstream Boutique manifest, license and single-service dependency configuration |
| `sentinel/` | Sentinel Kubernetes resources and restricted Executor RBAC |
| `kind/` | Local cluster configuration, including the separate Calico security lab |
| `docker/` | Sentinel component, security inspector and packaged-demo Dockerfiles; repository-root build context |
| `workloads/` | Small demo workload Kubernetes manifests |
| `chaos-mesh/` | Pinned lab installation values; see `docs/chaos.md` |

Workload Dockerfiles remain alongside their application sources in `experiments/workloads/`. The Verifier lab uses the repository root as its build context and installs its own SDK requirements:

```powershell
docker build -f experiments/workloads/verifier-lab/Dockerfile -t sentinel-verifier-lab:0.1.0 .
```

The small `experiments/workloads/demo/` and `experiments/workloads/otel/` applications keep their own directories as build contexts. The security candidate Dockerfile and shipping protocol live in `experiments/workloads/security/`; `scripts/security/build_security_candidate.py` prepares its upstream build context.
