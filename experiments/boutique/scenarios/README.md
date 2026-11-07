# Live fault scenarios

The six JSON specifications are loaded by `harness/chaos.py`, which adds a namespace, one-pod service selector, duration and unique ownership label. Use `python scripts/boutique/run_boutique_chaos.py --dry-run` to preview them.

These live-demo faults do not define the RCAEval labels. See [the demo guide](../../../docs/boutique/boutique-chaos-demo.md) and [offline benchmark](../../../docs/evaluation/rcaeval.md).
