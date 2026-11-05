# Repository layout

The shared experiment environment separates reusable workload definitions from collected evidence.

| Path | Responsibility |
| --- | --- |
| `services/` | Agent services grouped by role, each with core logic and supporting modules |
| `runtime/`, `agent/`, `shared/` | Runtime pipelines, RCA engine and shared contracts |
| `dashboard/` | Frontend |
| `experiments/boutique/` | Shared Boutique harness, traffic, scenarios/profiles and evaluation requirements |
| `experiments/workloads/` | Small Executor and Verifier lab applications |
| `deploy/` | Deployment resources, RBAC, Dockerfiles and local cluster settings |
| `data/sentinel/` | Sentinel fixtures, policies, configuration and ignored runtime outputs |
| `data/experimentation/` | Benchmark datasets, model settings, schemas and ignored experiment results |
| `tests/` | Existing offline and live test coverage |
| `scripts/` | Public launch commands and compatibility wrappers |
| `runbooks/` | Recovery knowledge |
| `docs/` | Setup, architecture, component guides and validation records |

## Migration performed

`infra/` resources moved into `deploy/` by purpose. Boutique implementation code moved out of `scripts/` into `experiments/boutique/harness/` and `traffic/`. Old Python script imports and launch commands remain compatible. Demo application sources moved from `demo/` into `experiments/workloads/`. Launchers, Docker build inputs and documentation now use the new asset paths. Pinned upstream manifest bytes and license are preserved.

Agent deployment manifests live in `deploy/sentinel/`. Service implementation modules use these packages:

```text
services/
  coordinator/core.py
  observer/       # core.py, cli.py, kubernetes.py, kubernetes_adapter.py
  planner/core.py
  rca/            # core.py, cli.py
  guard/          # core.py, cli.py, adapters.py, common.py, decisions.py,
                  # eligibility.py, preview.py, resources.py
  executor/       # core.py, store.py, kubernetes_adapter.py
  verifier/       # core.py, cli.py, http.py, live.py, live_cli.py,
                  # live_worker.py, store.py, traces.py
```

Import core interfaces from `services.<role>.core`, for example `services.planner.core.propose`. Run CLIs with their package paths, such as `python -m services.guard.cli` or `python -m services.rca.cli`. The top-level `agent/` package remains the RCA engine used by these services and the evaluation harness.

## Remaining implementation

The shared Chaos Mesh runner now provisions the dedicated lab, executes six fault types, runs in-cluster shoppers and exports application logs, resource measurements and Kubernetes observations. See [the demo guide](boutique-chaos-demo.md). Offline evaluation uses [RCAEval RE1/RE2](rcaeval.md); custom dataset generation has been removed. Distributed traces and dashboard integration for the new fault controls remain unimplemented; live injection still needs target-cluster validation.

Existing fixtures, test locations and component documentation are retained. Do not add new Boutique implementation code to compatibility wrappers.
