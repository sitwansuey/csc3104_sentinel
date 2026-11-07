# Experiments

Experiment harnesses, workload applications and traffic drivers live here. Sentinel's production components remain in `services/`, `runtime/` and `agent/` during the incremental package migration.

- [Boutique](boutique/README.md): shared environment for the existing recovery demonstration and Chaos Mesh live demonstrations.
- `workloads/demo/`: small HTTP workload used by the Executor lab.
- `workloads/verifier-lab/`: instrumented HTTP workload used by the Verifier lab.

Deployment resources live in `deploy/`; generated evidence belongs in ignored `data/experimentation/runs/`. Public launch commands are grouped by task under `scripts/`; see the [command index](../scripts/README.md).
