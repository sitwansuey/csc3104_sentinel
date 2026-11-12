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
Live agent-mode runs are committed as evidence. They cover one fault type, where
ROLLBACK is always the right action, with pre-authorised lab approval, so they
do not show diagnosis accuracy in general. Owner acceptance is tracked in #56.

<!-- BEGIN GENERATED:claim-c-agent-warm -->
> **C-AGENT-WARM:** qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix.
> Scope: Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-warm -->

The runs above have inferred provenance. Three later runs were stamped with
their source revision:

<!-- BEGIN GENERATED:claim-c-agent-recorded -->
> **C-AGENT-RECORDED:** At a recorded revision, qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs.
> Scope: Three runs only, on a warm model: each bundle's model-runtime.json records qwen3:14b Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call loaded in under 1 s. The preflight negative test that ran just before is counted in C-AGENT-PREFLIGHT-RECORDED, not here. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC.
<!-- END GENERATED:claim-c-agent-recorded -->

<!-- BEGIN GENERATED:claim-c-agent-all -->
> **C-AGENT-ALL:** Across every recorded agent-mode attempt, 14/24 reached RECOVERED.
> Scope: Includes deliberate negative tests (unreachable model server, missing model) and runs on older revisions. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred for the 20 runs before 4afc879 and recorded for the 4 runs at 4afc879.
<!-- END GENERATED:claim-c-agent-all -->

[Testing](docs/testing.md) explains how to validate each mode and distinguish
live recovery from injected-model tests.

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
does not deploy six independent Sentinel microservices. The Executor experiment
and the Boutique harness (walkthrough, provider and agent modes) approve through
the [authenticated approval service](docs/approvals.md#live-harness-approval):
a person types the action ID, or an unattended run opts in to recorded automated
lab approval. The Verifier acceptance script still uses local lab approval. No
live run has used this path yet; the committed live evidence used pre-authorised
lab approval. Deployed
acceptance and cross-host execution ownership remain unfinished; the
[known limitations](#known-limitations-and-open-defects) list what that means
in code. See [architecture](docs/architecture.md) for the exact boundaries.

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
| `evidence/` | Sanitised, append-only run bundles, their index and the claims registry |

Generated databases, logs, screenshots, model artifacts and dataset downloads
are ignored; never commit credentials. Sanitised run evidence is committed under
[evidence/](evidence/README.md): bundles are append-only, copied through an
allowlist, hashed and labelled `recorded` or `inferred`. Results tables in
[docs/results.md](docs/results.md) are generated from them and checked in CI.

## Validate and extend

```powershell
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

Install the [test dependencies](docs/testing.md) first. Synthetic tests do not
prove live Kubernetes recovery. [Testing](docs/testing.md) also documents the
browser-driven live run and specialised role checks.

Optional work stays separate: [Chaos Mesh](docs/chaos.md) injects/collects faults
without claiming Sentinel repair (claim `C-CHAOS-ALL`); [security](docs/security.md)
is a protected lab for the shipping path only, not yet combined with guarded
recovery (claim `C-SECURITY-LAB`); [RCAEval](agent/datasets/README.md) measures
offline diagnosis, not recovery, and has no committed result yet; [packaging](docs/packaging.md)
describes the packaged runner (claim `C-PACKAGED`). The runner was rebuilt and
scanned for PR #98, but there is no tagged release image yet. Claims and their
scope caveats are listed in [results](docs/results.md#claims).

## Known limitations and open defects

Sentinel is a single-host prototype. This list is generated from
[docs/limitations.json](docs/limitations.json).
[docs/limitations.md](docs/limitations.md) gives each entry's impact, workaround
and the code or evidence it cites, and CI fails when a citation stops resolving.

<!-- BEGIN GENERATED:limitations-summary -->
- [L-CLOCK-ORDERING](docs/limitations.md#l-clock-ordering): Lifecycle ordering compares wall-clock timestamps written by different components (defect, open; planned P3.03)
- [L-OBSERVER-ZERO-REPLICAS](docs/limitations.md#l-observer-zero-replicas): A Deployment scaled to zero replicas is treated as healthy (defect, open; planned P5.07)
- [L-EXEC-WEDGE](docs/limitations.md#l-exec-wedge): A write rejected with its resource version unchanged waits in NEEDS_RECONCILIATION unless fencing is on (defect, mitigated; planned P2.06, P5.08)
- [L-EXEC-REDELIVERY](docs/limitations.md#l-exec-redelivery): A replacement Executor without the ledger row reported CONFLICT for a write that was applied (defect, mitigated; planned P3.01, P4.01)
- [L-AGENT-JOURNAL](docs/limitations.md#l-agent-journal): Agent mode bypasses the durable Planner journal (defect, mitigated; planned P2.01)
- [L-AGENT-APPROVAL](docs/limitations.md#l-agent-approval): No live run yet shows a person approving through the authenticated API (defect, mitigated; planned P2.02, P2.03)
- [L-RAW-LOGS-PROMPT](docs/limitations.md#l-raw-logs-prompt): Sentinel's planning prompts carry log text only cleaned and quoted, and the injection gate is a pattern list no live run has exercised (defect, mitigated; planned P2.04, P2.05, P4.04)
- [L-NO-LOGGING](docs/limitations.md#l-no-logging): Structured logs cover only the networked entrypoints, and no live run has collected them (defect, mitigated; planned P1.07, P3.02)
- [L-SINGLE-COORDINATOR](docs/limitations.md#l-single-coordinator): The Coordinator is one process with one SQLite file (limitation, open; planned P3.06, P4.02, P5.01)
- [L-HOST-LOCAL-EXECUTOR](docs/limitations.md#l-host-local-executor): The Executor ledger and target locks are host-local (limitation, open; planned P3.04)
- [L-NO-FENCING](docs/limitations.md#l-no-fencing): No fencing of abandoned or revoked writes across hosts (limitation, open; planned P2.06, P3.04, P3.05)
- [L-SINGLE-HOST-DISTRIBUTION](docs/limitations.md#l-single-host-distribution): The networked worker layer has only run on one host (limitation, open; planned P3.01, P4.01)
- [L-ONE-FAULT-ONE-ACTION](docs/limitations.md#l-one-fault-one-action): One fault type and one repair action (limitation, open; planned P2.08, P3.08, P3.09)
- [L-NO-DIAGNOSIS-ACCURACY](docs/limitations.md#l-no-diagnosis-accuracy): No diagnosis-accuracy evaluation (limitation, open; planned P2.07, P4.04)
- [L-OVERLAPPING-FAULTS](docs/limitations.md#l-overlapping-faults): Only one fault at a time has been verified (limitation, open; planned P4.05)
- [L-SECURITY-SHIPPING-ONLY](docs/limitations.md#l-security-shipping-only): The security lab protects only the shipping path (limitation, open; planned P4.06, P5.04, P5.05)
- [L-UNGATED-CVES](docs/limitations.md#l-ungated-cves): Workload image CVEs raise alerts but gate nothing (limitation, open; planned P5.02, P5.03)
- [L-CHAOS-NOT-WIRED](docs/limitations.md#l-chaos-not-wired): Chaos Mesh faults are injection only, and HTTP and DNS faults do not run on this host (limitation, open; planned P4.03)
- [L-NO-RELEASE](docs/limitations.md#l-no-release): No tagged, scanned release image (limitation, open; planned P2.09, P2.10)
- [L-LAB-CERTS](docs/limitations.md#l-lab-certs): Security lab certificates are two-day fixtures with no rotation (limitation, open; planned P5.06)
- [L-MODEL-LATENCY](docs/limitations.md#l-model-latency): Model cold loads make agent planning slow and variable (limitation, mitigated; planned P2.01)
- [L-UNPINNED-BASE-IMAGES](docs/limitations.md#l-unpinned-base-images): Sentinel component images used unpinned base image tags (limitation, mitigated; planned P1.09)
- [L-EVIDENCE-INFERRED](docs/limitations.md#l-evidence-inferred): Most committed evidence has inferred provenance (limitation, mitigated; planned P2.09)

8 fixed entries stay in the register as history.
<!-- END GENERATED:limitations-summary -->
