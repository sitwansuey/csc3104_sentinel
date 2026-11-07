# Optional Chaos Mesh injection lab

The merged source includes six scoped Chaos Mesh scenarios. This runner deploys
Boutique, injects faults, measures shopping impact and retains evidence. It does
**not** invoke the Sentinel Planner/Guard/Executor recovery chain. Fault expiry,
Kubernetes self-healing and harness cleanup are not Sentinel repair outcomes.

Install the core Docker/kind/kubectl/Python tools plus Helm on PATH. The runner
pins Chaos Mesh 2.8.2 and uses the dedicated `sentinel-boutique-chaos` cluster,
containerd socket and namespace filtering in `deploy/chaos-mesh/values.yaml`.
It must not be pointed at production or an unrelated namespace.

```powershell
python scripts/run_boutique_chaos.py --dry-run
python scripts/run_boutique_chaos.py --setup --faults network
```

The first command checks configuration without injection. The second creates
the dedicated lab and prompts before each selected fault. `--kind`, `--helm`
and `--kubectl` can select executable paths.

| Fault | Default target | Injection |
| --- | --- | --- |
| CPU | shippingservice | One stress worker, 90% load |
| Memory | recommendationservice | One 256 MB worker |
| Pod | shippingservice | Timed Pod failure |
| Network | shippingservice | 200 ms delay, 20 ms jitter |
| HTTP | frontend | Abort GET `/` on 8080 |
| DNS | frontend | Fail catalog DNS lookups |

`--target` chooses another supported service; HTTP requires frontend and Redis
does not support the selected DNS scenario. Namespace selectors, one-Pod mode
and 5..3600-second durations are validated. DNS caches/connections can hide
impact; actual injection confirmation and observed failure are recorded separately.

Defaults are 30 seconds baseline, 60 fault and 30 recovery. CLI options bound
sampling/traffic/durations. Evidence under `data/experimentation/runs/` contains
logs, Kubernetes state/events, actual kubelet usage, shopper timings, fault
status and cleanup. Missing measurements stay missing. This runner does not
collect distributed traces or generate the published RCAEval dataset.

Normal interruption attempts ownership-checked fault deletion, waits for
finalizers and stops its traffic/collection. The cluster remains for reuse.
After inspection, remove the dedicated cluster explicitly:

```powershell
kind delete cluster --name sentinel-boutique-chaos
kind get clusters
```

Abrupt host termination can leave a fault/lock; inspect the run and
`sentinel-chaos-demo-lock` before resuming. Never strip finalizers to claim clean
restoration. Offline scope/lifecycle tests pass; no new live Chaos Mesh acceptance
was performed in this review. Its integration with guarded recovery remains pending.
