# Sentinel

Sentinel diagnoses Kubernetes incidents and checks recovery proposals before
restricted execution. Kubernetes then reconciles the repaired configuration;
Sentinel's Verifier independently decides whether service health recovered.

The current local experiment deploys eleven Google Online Boutique Deployments,
observes the connected storefront and checkout, and injects/repairs shipping
configuration faults. Planning is selectable: reproducible harness proposals or
the RCA agent with an Ollama Recovery Planner. Model output never grants execution
authority. **APPLIED does not mean RECOVERED.**

## Install, run, inspect, clean up

Follow [setup](docs/setup.md) for script or manual installation. Docker Desktop
must run its Linux engine. From this repository's root in Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1 -CheckOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
```

Open **http://127.0.0.1:8765/#overview** for Sentinel and
**http://127.0.0.1:8080** for Boutique. After the healthy baseline is ready, use
the three ordered injection buttons. The default harness mode needs no model,
benchmark dataset or cloud account. Press Ctrl+C after inspection and allow the
launcher to finish its scoped cleanup.

For Linux/macOS source launch, after installing the same tools:

```bash
bash scripts/run-sentinel.sh --setup --cleanup
```

Windows/amd64 is the current live-validated platform. Read the architecture and
resource notes in [setup](docs/setup.md) before using Apple Silicon.

[Run and observe](docs/run.md) explains manual/automatic injection, evidence,
the files used at each stage and interrupted cleanup. [Planner mode](docs/planner.md)
adds `-Agent`; it requires a reachable Ollama server and a model you installed.
Real-model acceptance is still pending. [Current verification status](docs/status.md)
distinguishes actual Kubernetes tests from injected-model tests.

## Team responsibilities

| Role | Responsibility | Implementation guide |
| --- | --- | --- |
| 1 Coordinator | Incident lifecycle, approvals, durable records and routing | [Coordinator](services/coordinator/README.md) |
| 2 Observer | Kubernetes/telemetry collection and sustained failure detection | [Observer](services/observer/README.md) |
| 3 Planner | Read-only RCA investigation and validated recovery proposals | [Planner](services/planner/README.md) |
| 4 Guard | Fresh state, policy, baseline, resources, dependencies and approval checks | [Guard](services/guard/README.md) |
| 5 Executor | Restricted conditional writes, idempotency, coordination and crash reconciliation | [Executor](services/executor/README.md) |
| 6 Verifier | Dependency assessments and sustained post-action outcomes | [Verifier](services/verifier/README.md) |

The current source runs these components through local workers/harnesses. It
does not deploy six independent Sentinel microservices. Demo approval is trusted
lab approval; authenticated approval and cross-host execution ownership remain
unfinished. See [architecture](docs/architecture.md) for the exact boundaries.

## Repository map

| Directory | Purpose |
| --- | --- |
| `services/` | Role packages: decision logic, adapters and owned persistence |
| `runtime/` | Local worker handoffs, live composition and dashboard projections |
| `agent/` | Bounded RCA tools/knowledge and optional offline benchmark adapters |
| `deploy/` | kind configuration, Kubernetes/RBAC manifests, Dockerfiles, Chaos Mesh values |
| `experiments/` | Boutique harness, traffic probes, regression scenarios and small test workloads |
| `scripts/` | Supported launch/check entrypoints; specialised validation/security tools in subfolders |
| `data/` | Synthetic fixtures, policies, pinned benchmark metadata and Guard case inputs |
| `runbooks/` | Recovery guidance; distinct from diagnostic knowledge in `agent/knowledge/` |
| `tests/` | Meaningful component, concurrency, restart and cross-role regressions |
| `docs/` | Setup, operation, architecture, testing and optional-mode guides |

Generated databases, logs, screenshots, model artifacts and dataset downloads
are ignored. Preserve/share sanitised evidence separately; do not commit credentials.

## Validate and extend

```powershell
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

Install the [test dependencies](docs/testing.md) first. Synthetic tests do not
prove live Kubernetes recovery. [Testing](docs/testing.md) also documents the
browser-driven live run and specialised role checks.

Optional work stays separate: [Chaos Mesh](docs/chaos.md) injects/collects faults
without claiming Sentinel repair; [security](docs/security.md) is a protected
lab awaiting recovery integration; [RCAEval](agent/datasets/README.md) measures
offline diagnosis, not recovery; [packaging](docs/packaging.md) describes the
existing runner and its pending rebuild/scan. No old image is the current release.
