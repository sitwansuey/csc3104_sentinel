# Experiments and validation support

- `boutique/`: live shipping/connected harness, shopper traffic, profiles and
  separate Chaos Mesh injection scenarios.
- `distributed/`: the Coordinator and workers as separate processes on one host,
  with a fault proxy, fault points and invariant checks
  ([guide](distributed/README.md)).
- `recovery/`: shared restricted-execution experiment implementation; supported
  launchers stay under `scripts/`.
- `validation/`: deterministic component demonstrations used by regression tests.
- `workloads/`: small HTTP/SDK/security fixtures used by specialised acceptance
  tests. The actual Boutique manifest is under `deploy/boutique/`.

This code provisions/injects/collects experiments. Recovery decision logic,
persistence and Kubernetes mutation boundaries live in `services/` and `runtime/`.
Use [the operating guide](../docs/run.md) for the normal application flow.
