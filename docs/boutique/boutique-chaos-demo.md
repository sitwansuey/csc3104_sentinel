# Boutique Chaos Mesh live demo

Chaos Mesh remains available for live demonstrations. Dataset generation and dataset export have been removed; offline evaluation uses the published RCAEval RE1/RE2 cases instead.

```powershell
python scripts/boutique/run_boutique_chaos.py --setup --faults network
```

Prerequisites: Python, kubectl, kind and Helm on PATH, plus Docker Desktop running Linux containers. Setup provisions the dedicated `sentinel-boutique-chaos` lab with Boutique v0.10.7 and Chaos Mesh 2.8.2. Omit `--setup` when it already exists. `--kind`, `--helm` and `--kubectl` accept executable paths.

```powershell
python scripts/boutique/run_boutique_chaos.py --dry-run
python scripts/boutique/run_boutique_chaos.py --faults cpu memory pod network http dns
```

Each selected fault runs once, with an Enter gate before injection. There is no repetition count, dataset mode or dataset export option. The existing invalid-PORT recovery demo remains available through `scripts/boutique/test-boutique-live.ps1`.

| Fault | Default target | Behavior |
| --- | --- | --- |
| CPU | shippingservice | One stress worker at 90% load |
| Memory | recommendationservice | One worker allocating 256 MB |
| Pod | shippingservice | Timed pod failure |
| Network | shippingservice | 200 ms delay with 20 ms jitter |
| HTTP | frontend | Abort GET `/` at container port 8080 |
| DNS | frontend | Fail productcatalogservice DNS lookups |

`--target` changes the service, except HTTP requires the frontend. DNS caching and persistent connections may mask application impact; confirmed injection is recorded separately from measured failures.

The defaults are a 30-second baseline, 60-second fault and 30-second recovery window. `--baseline-seconds`, `--fault-seconds`, `--recovery-seconds`, `--sample-interval`, `--traffic-interval` and `--traffic-workers` adjust the demonstration.

Run evidence stays under `data/experimentation/runs/boutique-chaos-<id>/`: application logs, Kubernetes state/events, kubelet CPU/memory measurements, shopper request timings, fault status and cleanup outcomes. These are demonstration artifacts, not the offline benchmark. Log polling is bounded and overlapping; unavailable logs and collection gaps are recorded. Distributed traces are not collected by this demo.

The runner checks fault ownership before deletion, waits for finalizers, attempts cleanup on Ctrl+C/SIGTERM and stops after a failed demonstration. The cluster remains for reuse. Abrupt host termination can leave resources; inspect the run evidence and `sentinel-chaos-demo-lock` before resuming. Older runs may retain the former `sentinel-dataset-lock` name and will need manual inspection.

The existing dashboard buttons still use invalid-PORT scenarios; the six new Chaos Mesh controls are CLI-driven. No Sentinel repair is claimed when a fault expires or the harness cleans it up.

Live execution remains unverified in this workspace because the Docker Linux engine was stopped and kind/Helm were not on PATH at implementation time. The manifests were checked against the pinned Chaos Mesh CRDs and lifecycle tests run offline.

See [RCAEval import and evaluation](../evaluation/rcaeval.md) for the independent published benchmark.
