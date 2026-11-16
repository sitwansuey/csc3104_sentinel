# Chaos Mesh on the dedicated kind lab

`values.yaml` configures containerd at `/run/containerd/containerd.sock`, one controller replica, namespace opt-in filtering and the Chaos DNS server. The demo runner pins Helm chart version 2.8.2 and installs it into `chaos-mesh` when invoked with `--setup`.

```powershell
python scripts/run_boutique_chaos.py --setup --faults network
```

Only the dedicated `kind-sentinel-boutique-chaos` context and `sentinel-boutique-chaos` namespace can be provisioned by setup. The same pinned chart and values are installed into the connected lab, install only, by `python scripts/boutique_chaos_recovery.py install` (`kind-sentinel-boutique-connected`, namespace `sentinel-boutique-connected` opted in); see [Chaos Mesh in guarded recovery](../../docs/chaos.md#chaos-mesh-in-guarded-recovery). Scenario definitions are in `experiments/boutique/scenarios/`. Existing other lab contexts can be used explicitly after installation and namespace opt-in, but setup will not rewrite them.

The four-node `kind-sentinel-dist` lab (P4.01) installs the same chart version with these values through the explicit `helm upgrade --install` command in its [recipe](../../docs/distributed-acceptance.md#live-recipe); its `sentinel-dist` namespace is annotated for injection by its own manifest.

See [the live demo guide](../../docs/chaos.md). Live kernel/runtime compatibility must be verified on the target Docker/kind environment. On Docker Desktop's WSL2 kernel, HTTPChaos cannot inject because the `ebtables` module is missing; DNSChaos is refused for the pinned Boutique pods because their root filesystems are read-only.
