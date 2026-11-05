# Online Boutique live experiments

The six Chaos Mesh fault scenarios are retained for live demonstrations. Custom dataset generation, repetitions and dataset export have been removed. Offline diagnosis evaluation uses the published [RCAEval non-code subset](../../agent/datasets/README.md).

```powershell
python scripts/run_boutique_chaos.py --setup --faults network
python scripts/run_boutique_chaos.py --dry-run
```

The CLI records a baseline, waits for Enter, injects each selected fault once, captures local telemetry and verifies cleanup/recovery. Evidence stays under `data/experimentation/runs/`. See [the live demo guide](../../docs/chaos.md).

`harness/chaos_demo.py` owns the live lifecycle; `harness/chaos.py` builds/provisions fault resources; `harness/collection.py` collects telemetry; `traffic/worker.py` runs in-cluster shoppers. `profiles/demo.json` contains the timing defaults.

The earlier invalid-shipping-PORT / guarded rollback demonstration remains in `harness/live.py`, `connected.py` and `dashboard.py`, with existing script compatibility entry points. Its dashboard controls are separate from the six Chaos Mesh CLI scenarios.
