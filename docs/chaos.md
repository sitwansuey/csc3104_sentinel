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
and `--kubectl` can select executable paths. `--dry-run` checks only local
scope and duration rules; Chaos Mesh admission rules are checked by the
server-side preflight once the lab exists.

| Fault | Default target | Injection |
| --- | --- | --- |
| CPU | shippingservice | One stress worker, 90% load |
| Memory | recommendationservice | One 256 MB worker |
| Pod | shippingservice | Timed Pod failure |
| Network | shippingservice | 200 ms delay, 20 ms jitter on traffic leaving the target |
| HTTP | frontend | Abort GET `/` on 8080 |
| DNS | frontend | Fail catalog DNS lookups |

Without `--faults`, the batch runs CPU, memory, Pod and network. HTTP is
opt-in (`--faults http` or `all`): the Chaos Mesh HTTP proxy needs the
`ebtables` kernel module, which the Docker Desktop WSL2 kernel does not
provide. There the injection is never confirmed and the HTTPChaos object can
stay Terminating until the lab cluster is deleted. DNS is refused before any
cluster work: DNSChaos copies `/etc/resolv.conf` with a shell inside the target
container, and the pinned Boutique pods have read-only root filesystems (the
frontend image has no shell either).

`--target` chooses another supported service; HTTP requires frontend.
Namespace selectors, one-Pod mode and 5..3600-second durations are validated.
Actual injection confirmation and observed failure are recorded separately.

Defaults are 30 seconds baseline, 60 fault and 30 recovery. Before the
recovery window starts, the runner waits up to 90 seconds
(`--settle-timeout-seconds`, 0 disables) until the latest three shopping
journeys that started after the rollout passed: callers such as the frontend
can keep failing for several seconds while they reconnect to a restarted
service. The wait and the journeys that failed during it are recorded under
`settle` in `outcome.json`. If the wait times out, the recovery window starts
anyway and its normal all-journeys-pass rule decides. CLI options bound
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
restoration. On 11 November the network, Pod and CPU faults each completed on
Docker Desktop (Windows, WSL2 kernel) with injection and cleanup confirmed and a
healthy recovery window; the memory fault completed in an earlier run. Its
integration with guarded recovery remains pending.
