# Sentinel

An application-agnostic Kubernetes platform for AI-assisted incident diagnosis
and guarded recovery. Recovery proposals are checked against current state,
policies and dependencies, then verified against post-action health.

**First milestone:** failed rollout → incident → proposal → Guard → approval →
conditional execution → verified recovery. Start with one workload and one
action: restore a specific saved healthy Pod template (`ROLLBACK`).

## Current starter

The demo lab, message validator, synthetic samples and contract tests are provided.
The six service files are implementation stubs. Live adapters, service runtimes,
messaging and automated recovery still need to be built. Sample results are not
live authorisations or measured recovery results.

## Ownership and deliverables

| Member / component | First implementation | Finished component |
| --- | --- | --- |
| **1 — [Coordinator](services/coordinator.py) + environment** | SQLite incident lifecycle, approval records and local event routing; support the lab | Persistent lifecycle and approvals, reliable event routing, restart handling, shared workload/fault automation |
| **2 — [Observer](services/observer.py)** | Convert sample observations into incidents; filter transient failures and duplicates | Live Kubernetes and telemetry collection, threshold-based detection and correlated incident context |
| **3 — [Planner](services/planner.py)** | Retrieve generic runbooks and validate proposals from an injected mock provider | RAG-assisted LLM planning, typed actions, evidence references and explicit failure/escalation paths |
| **4 — [Guard](services/guard.py)** | Check target/action policy, current state, baseline and approval | State, policy, resource and dependency validation; ALLOW/REJECT/ESCALATE decisions; approval and optional preview gates |
| **5 — [Executor](services/executor.py)** | Restore an explicit template through a fake API; persist attempts and handle duplicates/conflicts | Restricted Kubernetes execution, per-target coordination, conditional writes, idempotency, bounded retries and crash reconciliation |
| **6 — [Verifier](services/verifier.py)** | Traverse a sample dependency graph and verify a health window | Observed dependency graph, impact-check results, sustained service/dependency health verification and recovery metrics |

All members own working code, meaningful tests and integration for their component.
Member 6 supplies dependency results; Member 4 makes the final Guard decision.
Member 1 records approval; Member 5 rereads state and obtains fresh Guard validation
under execution coordination. Member 6 confirms outcomes before Member 1 marks
an incident recovered. Human approval does not override a hard rejection.

Assign names and reviewers before coding. The technical lead coordinates contracts,
architecture decisions and integration; implementation remains shared across all six.

## Roadmap and integration gates

No fixed dates: progress is measured by the deliverables below. Work can overlap,
but each gate must pass before that capability is treated as complete.

| Phase | Progress deliverable | Integration / exit gate |
| --- | --- | --- |
| **1. Foundation** | Agree on owners, message formats, action/lifecycle states, approval semantics and one recovery scenario | Everyone can run starter checks and identify their producer/consumer interfaces |
| **2. Offline recovery loop** | All six implement fixture-driven logic using mock planning, fake APIs, persistence and health checks | One scenario passes through every component; rejection, missing approval, duplicates and stale-state paths are tested |
| **3. Live Kubernetes** | Connect observation, conditional execution and health adapters; configure restricted RBAC; validate the demo, then introduce Online Boutique | Sentinel detects and recovers a real injected faulty rollout; rejected/unapproved actions make no cluster write |
| **4. Distributed services** | Add service entrypoints, containers, deployments, asynchronous messaging and suitable durable storage; implement coordination and retries | End-to-end flow runs across separate processes; duplicate delivery, competing workers, worker restarts and execution crash windows are tested |
| **5. Research capabilities** | Connect RAG/LLM planning, OpenTelemetry and trace-derived dependencies; expand allowlisted actions; add selected recovery previews as a separate optional increment | Integrate each capability separately; existing recovery tests remain passing and new validation decisions are demonstrated |
| **6. Evaluation and handover** | Compare recovery configurations, run repeated faults and a second workload, consolidate metrics and startup instructions | Final system regression passes; experiments are reproducible and limitations are recorded |

Member 3 can build the LLM adapter while mock planning is integrated. Member 6
can build trace parsing while fixture-based dependencies are integrated. The lab
must not block everyone else's first PR.

Controlled previews are optional in the proposal. If implemented, evaluate them
explicitly; if omitted, record that limitation. Do not count an unimplemented
preview as part of the full evaluated configuration.

## Integration and completion rules

Integrate throughout development. Final testing confirms the combined system;
it must not be the first time components are connected.

- **Every PR:** component tests, shared contract checks and a neighbouring-component check where applicable.
- **Every phase:** a complete recovery-flow check appropriate to that phase's mock/live/distributed setup.
- **Final gate:** deployed end-to-end regression, distributed failure cases and evaluation runs.

Add the first scenario integration test during Phase 2 and maintain it as adapters
change. The current CI checks contracts, discovered Python tests and lab-script
syntax; it does not yet test a live cluster or the complete recovery pipeline.

Required recovery checks include:

- Rejected or unapproved actions produce no write.
- Duplicate action IDs do not repeat effects; changed payloads under one ID are rejected.
- Stale state triggers reread/revalidation; competing writes cannot both commit from the same version.
- A crash after the write but before ledger completion is reconciled from live state/action evidence.
- An accepted API write with unhealthy outcomes is not marked recovered.

A finished component has an executable runtime, agreed inputs/outputs, required
configuration, successful integration, meaningful failure-path tests and concise
startup instructions. Each owner supplies their service's container/deployment
changes; Member 1 coordinates the shared environment.

## Evaluation target

Compare **RAG-assisted recovery without Guard validation**, **RAG plus a state/policy
Guard**, and **full Sentinel with dependency and outcome-aware recovery**. Measure
action validity, recovery success, unsafe-action rejection, false rejection,
latency and post-recovery health. Define safe/unsafe cases and health thresholds
before scoring; use the same independent outcome measurement across configurations.

Use Online Boutique as the primary workload and a second workload to test
application independence. Keep evaluation solutions out of `runbooks/`. Record
fault settings, workload/model/prompt versions and repeated-run results.

## Start coding

Use Python 3.12+. No external packages are needed for the starter checks.

```bash
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

Choose a service file and implement its TODOs. Add `tests/test_<component>.py`
and a small fixture-driven example. Use a feature branch, one reviewer and a
focused PR covering behaviour, validation and affected interfaces.

| Input | Purpose |
| --- | --- |
| [samples/observations.json](samples/observations.json) | Healthy, persistent-failure and transient sequences |
| [samples/messages/](samples/messages/) | Six wire-message examples validated by [shared/contracts.py](shared/contracts.py) |
| [samples/baseline.json](samples/baseline.json) | Exact healthy template and its identity/hash |
| [samples/policy.json](samples/policy.json) | Permissions, approval and detection/health thresholds |
| [samples/dependencies.json](samples/dependencies.json) / [samples/health.json](samples/health.json) | Synthetic graph and health readings |
| [runbooks/](runbooks/) | Generic retrieval guidance |

Keep IDs stable across retries and treat resource versions as opaque strings.
The coordinator and executor own separate records; a local SQLite prototype is
not distributed coordination across hosts. Workload constraints belong in policy,
not hard-coded application logic.

## Local lab

With Docker, kind and kubectl available, run from the repo root. Inspect the fault
before restoring it; the fault command's rollout timeout is expected.

```bash
bash scripts/lab.sh up
bash scripts/lab.sh health
bash scripts/lab.sh fault
bash scripts/lab.sh inspect
bash scripts/lab.sh recover
bash scripts/lab.sh health
```

This is an operator-run baseline, not Sentinel's automated executor. Lab evidence
is saved under `runs/`; copy it before repeating experiments you want to retain.

## Architecture target

One shared repo, with independently deployable services communicating through
versioned events/APIs. Keep service logic and owned data private. Share wire-contract
checks rather than importing another service's business logic. Six ownership areas
do not require exactly six deployed services; agree on cohesive deployment boundaries.

The initial files are development scaffolds. Add runtime and deployment files as
their services become executable, and keep this README current as gates are passed.
