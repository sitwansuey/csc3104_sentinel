# Repository layout

Source, launch commands, deployment assets and collected evidence have separate homes. Run the commands in the guides from the repository root.

| Path | Responsibility |
| --- | --- |
| `services/` | Service implementations grouped by role, including their supporting modules |
| `runtime/` | Shared runtime pipelines, worker transport and dashboard backend |
| `agent/` | RCA engine, knowledge and benchmark adapters |
| `shared/` | Shared message contracts |
| `dashboard/` | React frontend, with component tests beside the components |
| `scripts/` | Launch commands grouped by task; see the [command index](../scripts/README.md) |
| `tests/` | Python tests grouped by feature, with shared fakes in `tests/support/` |
| `experiments/boutique/` | Shared Boutique harness, traffic, scenarios, profiles and evaluation requirements |
| `experiments/workloads/` | Small Executor, Observer, security and Verifier lab applications |
| `deploy/` | Deployment resources, RBAC, Dockerfiles and local cluster settings |
| `data/sentinel/` | Sentinel fixtures, policies, configuration and ignored runtime outputs |
| `data/experimentation/` | Benchmark datasets, model settings, schemas and ignored experiment results |
| `evaluation/` | Versioned Guard cases and preliminary evaluation results |
| `runbooks/` | Recovery knowledge |
| `docs/` | Guides grouped by feature, with dated reviews in `docs/archive/` |

## Documentation

| Folder | Guides |
| --- | --- |
| `setup/` | [Dependencies](setup/dependencies.md), [laptop quickstart](setup/laptop-demo-quickstart.md), [packaged demo](setup/packaged-demo.md), [project automation](setup/project-automation.md) |
| `boutique/` | [Demo handbook](boutique/boutique-demo-handbook.md), [connected lab](boutique/boutique-connected.md), [single-service lab](boutique/boutique-live.md), [Chaos Mesh demo](boutique/boutique-chaos-demo.md), [Observer acceptance](boutique/boutique-observer-acceptance.md) |
| `dashboard/` | [Dashboard](dashboard/dashboard.md), [API contract](dashboard/dashboard-api.md), [browser experiment controls](dashboard/experiment-controls.md) |
| `architecture/` | [Current system](architecture/current-system-demo.md), [Coordinator](architecture/coordinator.md), [offline flow](architecture/offline-flow.md), [provider recovery](architecture/provider-recovery.md), [RCA](architecture/rca.md), [worker transport](architecture/worker-transport.md) |
| `guard/` | [Recovery Guard](guard/recovery-guard.md), [owner guide](guard/guard-role.md), [runtime](guard/guard-runtime.md), [v2 rules](guard/guard-v2.md), [integration contract](guard/guard-integration-contract.md), [dependency revalidation](guard/dependency-revalidation.md), [rejection demo](guard/guard-rejection-demo.md) |
| `observer/` | [Runtime](observer/observer-runtime.md), [telemetry](observer/observer-telemetry.md), [live test](observer/observer-live-test.md) |
| `verifier/` | [Verifier](verifier/verifier.md), [durable worker](verifier/verifier-worker.md), [live evidence](verifier/verifier-live.md), [connected validation](verifier/verifier-connected-validation.md) |
| `executor/` | [Executor](executor/executor.md), [live execution](executor/executor-live.md) |
| `security/` | [Inspection and CVEs](security/security.md), [connected lab](security/security-connected.md), [image security](security/image-security.md) |
| `evaluation/` | [Guard evaluation](evaluation/guard-evaluation.md), [RCAEval](evaluation/rcaeval.md) |
| `archive/` | [5 November consolidation review](archive/repository-review-2026-11-05.md); historical claims and evidence paths describe that review |

## Commands and tests

Scripts use `cli/`, `boutique/`, `executor/`, `observer/`, `verifier/`, `observability/`, `security/`, `demos/`, `evaluation/` and `setup/` folders. For example:

```powershell
python scripts/cli/check_contracts.py
python scripts/observability/dashboard.py --demo
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\boutique\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
python -m unittest discover -s tests -p "test_*.py" -v
```

Use these categorized paths for new commands and imports. Two root Python shims, `scripts/test_boutique_live.py` and `scripts/guard_rejection_demo.py`, remain for the active live Boutique session, which can load them after startup. Their implementations live in `scripts/boutique/` and `scripts/demos/`.

Python tests use `boutique/`, `coordinator/`, `dashboard/`, `executor/`, `guard/`, `observer/`, `planner/`, `project/`, `rca/`, `recovery/`, `security/` and `verifier/` categories. Each category is a package so the existing discovery command continues to find the complete suite. Shared test helpers remain in `tests/support/`; frontend tests stay beside their components and library modules.

## Service packages

```text
services/
  coordinator/  # core.py, read.py
  observer/     # core.py, cli.py, kubernetes.py, kubernetes_adapter.py, telemetry.py
  planner/      # core.py
  rca/          # core.py, cli.py
  guard/        # core.py, cli.py, adapters.py, common.py, decisions.py,
                # eligibility.py, preview.py, resources.py
  executor/     # core.py, store.py, kubernetes_adapter.py
  verifier/     # core.py, cli.py, http.py, live.py, live_cli.py,
                # live_worker.py, store.py, traces.py
  security/     # grpc_inspection.py, image_security.py
```

Import core interfaces from `services.<role>.core`, such as `services.planner.core.propose`. Run service CLIs with their package paths, such as `python -m services.guard.cli` or `python -m services.rca.cli`.

Deployment manifests live in `deploy/sentinel/`. Component Dockerfiles use the repository root as their build context. Boutique implementation code belongs in `experiments/boutique/harness/` and `traffic/`; small lab applications belong in `experiments/workloads/`. The old `infra/`, `demo/` and root `samples/` layouts have been replaced by `deploy/`, `experiments/workloads/` and `data/sentinel/samples/`.
