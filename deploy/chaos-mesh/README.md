# Chaos Mesh on the dedicated kind lab

`values.yaml` configures containerd at `/run/containerd/containerd.sock`, one controller replica, namespace opt-in filtering and the Chaos DNS server. The demo runner pins Helm chart version 2.8.2 and installs it into `chaos-mesh` when invoked with `--setup`.

```powershell
python scripts/run_boutique_chaos.py --setup --faults network
```

Only the dedicated `kind-sentinel-boutique-chaos` context and `sentinel-boutique-chaos` namespace can be provisioned by setup. Scenario definitions are in `experiments/boutique/scenarios/`. Existing other lab contexts can be used explicitly after installation and namespace opt-in, but setup will not rewrite them.

See [the live demo guide](../../docs/boutique-chaos-demo.md). Live kernel/runtime compatibility must be verified on the target Docker/kind environment.
