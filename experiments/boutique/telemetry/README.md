# Live demonstration telemetry

The demo collects timestamped application logs, Kubernetes state/events, kubelet CPU/memory measurements and in-cluster shopper timings. It writes JSONL under `data/experimentation/runs/`; it does not export a generated dataset.

Log windows overlap, unavailable samples remain unknown, and gaps/overruns are recorded. Distributed traces are not collected by this demo. Published RCAEval telemetry is imported independently. See [the live guide](../../../docs/boutique/boutique-chaos-demo.md).
