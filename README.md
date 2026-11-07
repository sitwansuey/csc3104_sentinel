# Sentinel

An application-agnostic Kubernetes platform for AI-assisted incident diagnosis
and guarded recovery. Recovery proposals are checked against current state,
policies and dependencies, then verified against post-action health.

**First milestone:** failed rollout → incident → proposal → Guard → approval →
conditional execution → verified recovery. Start with one workload and one
action: restore a specific saved healthy Pod template (`ROLLBACK`).

## Current connected demo: start here

With Docker Desktop's Linux engine running and kind, kubectl, Python and the
dashboard build tools installed, run from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\boutique\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
```

Open **http://127.0.0.1:8765/#overview** for injection/recovery and
**http://127.0.0.1:8080** for Boutique. The connected demo uses Roles 1, 2, 4, 5
and 6 with harness-authored proposals and demo approval. Eleven Deployments are
observed; supported faults and repairs currently target shipping. The **Online
Boutique** page shows service readiness, shopping measurements, Pods/restarts
and warning events. The optional [live agent mode](docs/boutique/boutique-demo-handbook.md#live-agent-recovery-mode)
uses RCA and Ollama planning for one guarded recovery; the default demo keeps
harness-authored proposals.

Use the [demo handbook](docs/boutique/boutique-demo-handbook.md) and
[connected guide](docs/boutique/boutique-connected.md) for prerequisites, code used at
each stage and cleanup. The [5 November consolidation review](docs/archive/repository-review-2026-11-05.md)
records branch/PR decisions, exact tests, role coverage and remaining work.
The previously exported runner image predates these source changes.

## Repository organization

To test live recovery with your agent, see [live agent recovery mode](docs/boutique/boutique-demo-handbook.md#live-agent-recovery-mode): `python scripts/boutique/test_boutique_live.py --agent --dashboard --pause-before-injection`.

All project data is under `data/`: `data/sentinel/` contains application fixtures, settings and runtime evidence; `data/experimentation/` contains benchmark datasets, experiment settings and evaluation results. Existing `sample-100` results are at `data/experimentation/runs/sample-100/`. See the [data layout](data/README.md).

See the [repository layout](docs/repository-layout.md), [categorized commands](scripts/README.md) and [shared Boutique experiment environment](experiments/boutique/README.md). Scripts, Python tests and guides are grouped by feature. Deployment resources live in `deploy/`; Boutique harness and traffic code live in `experiments/boutique/`; dataset conventions live in `data/`. Use the categorized command paths shown below. The [Chaos Mesh demo guide](docs/boutique/boutique-chaos-demo.md) documents live faults; the [RCAEval guide](docs/evaluation/rcaeval.md) documents the published offline benchmark.

## RCAEval benchmark and live chaos demo

Offline evaluation uses **645 non-code RCAEval cases**: RE1 and RE2 across Online Boutique, Sock Shop and Train Ticket. RE3 code-level faults are excluded. Custom dataset generation has been removed.

```powershell
python -m pip install -r requirements.txt
python scripts/evaluation/download_rcaeval.py
python scripts/evaluation/evaluate_rcaeval.py export --out data/experimentation/runs/rcaeval-eval
python scripts/evaluation/evaluate_rcaeval.py run --tasks data/experimentation/runs/rcaeval-eval/tasks --out data/experimentation/runs/rcaeval-eval/results --method casetoolkit
python scripts/evaluation/evaluate_rcaeval.py score --answers data/experimentation/runs/rcaeval-eval/answers.jsonl --predictions data/experimentation/runs/rcaeval-eval/results/predictions.jsonl --out data/experimentation/runs/rcaeval-eval/scores
```

To test your local model with upstream CaseToolkit, install `requirements.txt` and run the evaluator with `--method casetoolkit`. Model settings default to `data/sentinel/samples/rca-config.json`; `--config data/experimentation/config/rcaeval-model.json` supplies overrides. Tool requests, results and scored predictions are saved for inspection.

See [RCAEval selection, model evaluation and scoring](docs/evaluation/rcaeval.md). For live demonstrations, use `python scripts/boutique/run_boutique_chaos.py --setup --faults network`; see [the demo guide](docs/boutique/boutique-chaos-demo.md).

## Start here: local setup on Windows

For the container-packaged Boutique demo, use the
[short Windows laptop instructions](docs/setup/laptop-demo-quickstart.md) or the
[detailed packaged demo guide](docs/setup/packaged-demo.md). That route needs Docker
Desktop and kind on the host; Python, kubectl and the built UI run in the image.
The instructions below remain the host-development route.

The exported 0.3.0 runner supports the earlier shipping-only demo. For the latest
connected storefront and UI controls, use the source command above until a new
runner release is built and validated.

Use this section for a new teammate's first run. Run commands in PowerShell from
the repository root, the folder containing this README. Clone the team repository
or download/extract it first; Git is only needed if you choose to clone it.

Choose either [Method 1: scripts](#method-1-install-and-set-up-with-scripts) or
[Method 2: manual setup](#method-2-install-and-set-up-manually). Both run the same
real Kubernetes Executor experiment; manual setup skips the dependency installer
and environment-setup wrapper, not the Executor's safety checks.

The local environment needs:

| Dependency | Purpose |
| --- | --- |
| Docker Desktop with a running Linux engine | Builds the demo image and runs the kind node |
| kind | Creates the local Kubernetes cluster |
| kubectl | Talks to the Kubernetes API |
| Python 3.12+ | Runs Sentinel's code, tests and local workers |
| WinGet / Microsoft App Installer | Required only for installing missing tools through Method 1 |
| Node.js 20.19+ or 22.12+ with npm | Optional; builds the [observability dashboard](#observability-dashboard) frontend on first run |

Install the Python packages and benchmark data below in addition to the tools.
The agent uses a local Ollama model; no cloud account or LLM API key is required.
Package installation, dataset downloads and the first container-image downloads
need network access. Docker Desktop also needs its supported Windows/WSL 2 and
hardware-virtualization prerequisites; the [dependency guide](docs/setup/dependencies.md)
links the platform requirements.

### Install Python packages and download RCAEval

After installing Python through either tool setup method, run from the repository root:

```powershell
python -m pip install -r requirements.txt
python scripts/evaluation/download_rcaeval.py
```

`requirements.txt` is the single Python dependency file for the agent, benchmark
tools and instrumented Verifier lab. The downloader saves all **645 RE1/RE2
non-code cases** (about **3.11 GB**) under
`data/experimentation/datasets/rcaeval-non-code/`; RE3 code faults are excluded.
It checks source hashes and reuses verified files when rerun. To fetch only the
filtered index, use `python scripts/evaluation/download_rcaeval.py --index-only`.
The underlying module also remains available as `python -m agent.datasets.rcaeval`.

Evaluation outputs belong under `data/experimentation/runs/`; application outputs
belong under `data/sentinel/runs/`. All directories named `runs/` are Git-ignored.

### Method 1: install and set up with scripts

#### 1. Install missing dependencies

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup\install-dependencies.ps1
```

Alternatively, double-click `scripts\setup\install-dependencies.bat`. The installer
keeps existing compatible tools and installs missing ones. Review installer
prompts and finish Docker Desktop's first-run setup. Windows feature changes,
WSL setup and reboots are not automated. If the tools install but Docker is not
running yet, the script reports that the engine is unavailable; continue below.

#### 2. Start Docker Desktop and check readiness

Start Docker Desktop yourself and use Linux containers with the WSL 2 backend.
The lab uses kind, so Docker Desktop's separate built-in Kubernetes cluster need
not be enabled. Then check without installing or creating anything:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup\install-dependencies.ps1 -CheckOnly
```

Expected result: all four tools are found and the Docker Linux engine responds,
with exit code 0. This checks prerequisites, not application recovery. These
scripts discover common installation locations; manual `docker`, `kind`,
`kubectl` and `python` commands require usable PATH entries. Reopen your terminal
after installation, or use the executable paths printed by the checker.
`-ExecutionPolicy Bypass` applies only to the launched PowerShell process.

#### 3. Run the offline checks

With a working Python command:

```powershell
python scripts/cli/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

These checks require no running cluster. Test-only API doubles reproduce failure
paths deterministically; passing them is not evidence of live Kubernetes recovery.

#### 4. Set up, test the live executor and clean up

For the complete local Role 5 experiment, use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\executor\test-executor-live.ps1 -Setup -Cleanup
```

This creates/reuses the single-node `sentinel-lab` kind cluster, builds and loads
the demo image, and deploys `demo-api` in namespace `sentinel-demo`. The experiment
captures a measured healthy baseline, creates restricted executor credentials,
deliberately injects demo startup failures, and tests actual conditional recovery,
rejections, duplicates, competing local workers and restart reconciliation after
a worker crash. Initial setup can take longer while images download.

The Python workers run on the desktop; Kubernetes runs the demo workload. The
experiment uses the real Coordinator, Guard and Executor, with operator-authored
proposals and a documented single-app dependency attestation. Planner/RAG and
Role 6 verification are still separate team work. `APPLIED` routes to `VERIFYING`,
not directly to `RECOVERED`.

With `-Cleanup`, the wrapper removes the named cluster after the test and retains
evidence in `data/sentinel/runs/executor-live-*`. The experiment removes its private executor
kubeconfig. Docker Desktop and cached images remain; cleanup does not prune
unrelated Docker resources. `data/sentinel/runs/` is Git-ignored, so preserve/share sanitized
experiment evidence separately.

To keep the healthy cluster available for development, omit `-Cleanup`:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\executor\test-executor-live.ps1 -Setup
```

Remove a retained cluster when finished. From a terminal where kind is available:

```powershell
kind delete cluster --name sentinel-lab
kind get clusters
```

The second command should no longer list `sentinel-lab`. The earlier validation
cluster was already removed; `-Setup` creates the environment again when needed.

### Method 2: install and set up manually

Install the programs yourself from their official sources. WinGet and the
dependency installer are optional for this route.

| Program | Manual installation/setup |
| --- | --- |
| [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) | Follow the Windows prerequisites, install it, finish first-run setup and start its Linux engine using WSL 2 |
| [kind](https://kind.sigs.k8s.io/docs/user/quick-start/) | Download the Windows binary for your CPU architecture, rename it `kind.exe` and place it in a directory on your user PATH |
| [kubectl](https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/) | Download the matching Windows executable, follow the upstream verification instructions and add its directory to your user PATH |
| [Python](https://www.python.org/downloads/windows/) | Install Python 3.12+ using an official installer or Install Manager, and make a working interpreter available as `python` |

To add a tools directory, open Windows' **Edit environment variables for your
account**, edit the user **Path**, add that directory and reopen PowerShell.
Keep kubectl within the supported version skew of your chosen Kubernetes node;
the upstream kubectl guide explains the compatibility requirement.

Check the installations from the repository root:

```powershell
docker version
docker info --format '{{.OSType}}'
kind version
kubectl version --client
python --version
```

Docker must report a reachable server and `linux`; Python must be 3.12 or newer.
Create the named cluster, build/load the image and deploy the workload explicitly:

```powershell
kind create cluster --name sentinel-lab --config deploy/kind/kind.yaml --wait 120s
docker build -t sentinel-demo:0.1.0 experiments/workloads/demo
kind load docker-image sentinel-demo:0.1.0 --name sentinel-lab
kubectl --context kind-sentinel-lab apply -f deploy/workloads/demo.yaml
kubectl --context kind-sentinel-lab --namespace sentinel-demo rollout status deployment/demo-api --timeout=120s
kubectl --context kind-sentinel-lab --namespace sentinel-demo get deployments,pods,services
```

If `sentinel-lab` already exists, inspect it with `kind get clusters` and reuse
that intended lab instead of creating it again. The Deployment should reach
`1/1` ready. Then run the actual Executor experiment against this prepared lab:

```powershell
python scripts/executor/test_executor_live.py
```

The experiment still measures the healthy baseline, provisions a restricted
executor identity, injects the faults and invokes the real Guard/Executor. It
does not use the old operator rollback as the recovery implementation. Expect
`PASS: live restricted executor and crash reconciliation`; Coordinator stops at
`VERIFYING`, with measured HTTP/replica checks recorded separately from Role 6.
Run the offline checks from Method 1 as well.

Clean up manually when finished:

```powershell
kind delete cluster --name sentinel-lab
kind get clusters
```

This removes the named cluster and its workload containers, while retaining
`data/sentinel/runs/` evidence and image caches. Both methods need Docker Desktop running
during live testing; neither requires enabling its separate Kubernetes cluster.

For detailed installation/troubleshooting and Linux/macOS prerequisites, read
[dependencies.md](docs/setup/dependencies.md). For the live Executor's design, inputs,
CLI, measured results, RBAC and limitations, read
[executor-live.md](docs/executor/executor-live.md). The [Local lab](#local-lab) section
below describes the older operator-run experiment and its menu separately.

## Current implementation

The opt-in [security lab](docs/security/security.md) adds an mTLS gRPC reverse proxy,
schema-aware shipping request inspection, image CVE detection and package patch
recommendations in the dashboard's Security view. It is isolated from existing
Kubernetes labs; scanning does not automatically patch or deploy images.
The [connected security lab](docs/security/security-connected.md) runs the full storefront
with inspected shipping traffic, enforced Calico policies, candidate validation
and scheduled image scans/alerts in its own kind cluster.

For opt-in browser buttons that inject the Boutique lab scenarios and display
their measured outcomes, see [browser-controlled experiments](docs/dashboard/experiment-controls.md).
The default dashboard remains read-only; the experiment controls do not approve
proposals or bypass Guard/Executor checks.
For connected-service testing with the real storefront and checkout, see the
[connected Boutique lab](docs/boutique/boutique-connected.md). It keeps shipping-only
Executor permissions and adds actual downstream shopping-flow verification.
Its **Online Boutique** dashboard page shows all eleven Deployment readiness
cards and measured homepage/cart/checkout results before, during and after faults.

The default Boutique injection sequence now ends with a Guard refusal
demonstration: normal rollback, executor-crash/reconciliation recovery, then an
explicitly labelled false recovery recommendation against the healthy
shippingservice. The last scenario rejects a redundant rollback with zero Executor
repair patch attempts; it does not crash the pod again. The default interactive
run has three Enter prompts and ends with two `RECOVERED` incidents and one
`REJECTED` incident. Rebuild the packaged runner to include the new sequence.
For only the final scenario, run `scripts\demos\start-guard-rejection.bat` or use
`-GuardRejection`. See [the Guard rejection demo](docs/guard/guard-rejection-demo.md).

For an opt-in experiment using the real Online Boutique shipping service, see
[the single-service Boutique guide](docs/boutique/boutique-live.md). It reuses the Guard
and restricted Executor with harness-authored proposals. Role 6 now evaluates
the reviewed dependency graph and publishes outcomes from live gRPC, readiness
and template evidence. Add `--agent` to use RCA and Ollama planning for one
recovery; the default three-scenario run uses harness-authored proposals.
The [Boutique demo instruction book](docs/boutique/boutique-demo-handbook.md) explains
integrated live dashboard startup, injection, role coverage, evidence and cleanup.
For team handover and presentations, see the
[current-system script and flowchart](docs/architecture/current-system-demo.md).

The demo lab, message validator, synthetic samples and contract tests are provided.
The Coordinator now has SQLite lifecycle/approval persistence, durable local event
routing, a CLI and an offline integration demo. The Observer has threshold-based detection, ownership-correlated Pod/event/log
collection, a persistent worker/outbox, local Coordinator handoff and a restricted
Kubernetes deployment. Its [connected telemetry runtime](docs/observer/observer-telemetry.md)
adds bounded OTLP traces/metrics/logs, current-Pod correlation, gauge thresholds
and independent monitoring of multiple configured Deployments. Boutique tracing
instrumentation and shared-cloud transport remain integration work.
The Guard has a fixture-driven evaluator,
expiring decisions bound to exact inputs, detailed reports,
baseline/security/resource checks, a JSON runner,
read-only Kubernetes metadata collection, optional server-side admission dry-run
and a container image definition, plus a durable local worker connected to the
Coordinator. The Executor has a persistent rollback core and a live Kubernetes
adapter with UID/resource-version conditional writes, atomic action evidence,
local per-target process coordination and conservative crash reconciliation.
The live runtime connects the core to the actual Guard and current approvals;
the local worker supports Coordinator result delivery. Test-only API doubles
are isolated under `tests/support/` for offline regressions. A read-only
[observability dashboard](#observability-dashboard) displays Coordinator records,
Guard reports, cluster metrics and fault markers. The Verifier now evaluates
offline dependency graphs and sustained post-action health windows, with
Guard/Coordinator integration tests. Offline Planner validation and a provider
adapter boundary are implemented; the default live demo still uses the harness.
Boutique now consumes durable verification requests and supplies real gRPC,
readiness and template evidence; successful outcomes close its incidents as
RECOVERED. See [the current workflow](docs/architecture/current-system-demo.md).
Trusted production baseline storage, authenticated human approvals, deployed
remote worker adapters and cross-host target ownership still need integration.
An optional authenticated worker-delivery interface exists; it is not the
transport used by the local harness or proof of distributed execution. Sample
results are not live authorisations or measured outcomes.

The [Executor core guide](docs/executor/executor.md) explains durable action records,
coordination and crash reconciliation. The
[live Executor write-up](docs/executor/executor-live.md) describes actual Kubernetes
execution, restricted RBAC, team handoff, reproducible testing and cleanup.

## Ownership and deliverables

| Member / component | First implementation | Finished component |
| --- | --- | --- |
| **1 — [Coordinator](services/coordinator/core.py) + environment** | SQLite incident lifecycle, approval records and local event routing; support the lab | Persistent lifecycle and approvals, reliable event routing, restart handling, shared workload/fault automation |
| **2 — [Observer](services/observer/core.py)** | Convert sample observations into incidents; filter transient failures and duplicates | Live Kubernetes and telemetry collection, threshold-based detection and correlated incident context |
| **3 — [RCA / Planner](docs/architecture/rca.md)** | Imported Sentinel RCA engine and diagnostic runbooks; validate proposals from an injected provider | Evidence-backed diagnosis, typed actions, evidence references and explicit failure/escalation paths |
| **4 — [Guard](services/guard/core.py)** | Check target/action policy, current state, baseline and approval | State, policy, resource and dependency validation; ALLOW/REJECT/ESCALATE decisions; approval and optional preview gates |
| **5 — [Executor](services/executor/core.py)** | Restore an explicit template through a fake API; persist attempts and handle duplicates/conflicts | Restricted Kubernetes execution, per-target coordination, conditional writes, idempotency, bounded retries and crash reconciliation |
| **6 — [Verifier](services/verifier/core.py)** | Traverse a sample dependency graph and verify a health window | Observed dependency graph, impact-check results, sustained service/dependency health verification and recovery metrics |

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
change. The current CI checks contracts, discovered Python tests, lab-script
syntax and the dashboard frontend build/tests; it does not yet test a live cluster
or the complete recovery pipeline.

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
application independence. Keep evaluation solutions out of `agent/knowledge/`. Record
fault settings, workload/model/prompt versions and repeated-run results.

## Start coding

Use Python 3.12+. No external packages are needed for the starter checks.

```bash
python scripts/cli/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/demos/demo_guard.py
python scripts/demos/demo_coordinator.py
python scripts/demos/demo_guard_handoff.py
python scripts/demos/demo_guard_v2.py
python scripts/demos/demo_verifier.py
```

The [Verifier guide](docs/verifier/verifier.md) explains graph traversal and health windows.
The [durable worker](docs/verifier/verifier-worker.md) preserves readings/deadlines through
restart; [live adapters](docs/verifier/verifier-live.md) read Kubernetes, HTTP and OTLP
evidence. The [connected validation](docs/verifier/verifier-connected-validation.md) records
real Collector, dependency-failure, timeout and worker-restart acceptance cases.

The [Coordinator guide](docs/architecture/coordinator.md) describes persistence, lifecycle,
approval and routing contracts, CLI usage and restart recovery. Its demo connects
the real Observer and Guard with synthetic planner/executor/verifier responses.
Executor regression tests exercise the real Coordinator, Guard and Executor
with test-only API doubles. Use the live experiment for measured Kubernetes
execution; APPLIED still requires independent outcome verification.

For installation and the actual live Executor experiment, follow
[Start here: local setup on Windows](#start-here-local-setup-on-windows).
The [live guide](docs/executor/executor-live.md) explains measured baseline evidence, the
restricted identity and the remaining production limits.

The [Observer runtime guide](docs/observer/observer-runtime.md) covers Buddy 2 startup,
read-only deployment, durable delivery, live restart evidence and remaining
completion gates.

The [Recovery Guard guide](docs/guard/recovery-guard.md) explains its local inputs,
resource constraints, temporary approval format and decision scenarios. The
[runner guide](docs/guard/guard-runtime.md) covers arbitrary JSON inputs, explicit live
Kubernetes reads, exit codes and the Guard container. The
[version-2 guide](docs/guard/guard-v2.md) covers strict live policy, health/security and
workload budgets, bound decisions, admission previews and audit reports. The
[Member 4 role guide](docs/guard/guard-role.md) describes your contribution and teammate
handoffs. The demos use synthetic fixtures and a fixed UTC clock; they make no
cluster writes. Version-2 demo approvals, health records and server previews are
fabricated and have no live authority. The
handoff demo exercises fresh validation and conditional writes against a fake API.
The [Guard evaluation guide](docs/evaluation/guard-evaluation.md) provides versioned safe/unsafe
cases, a state/policy versus full-validation runner, explicit metrics and preliminary
fixture results. Final scoring still requires the accepted frozen release.
The [dependency revalidation guide](docs/guard/dependency-revalidation.md) demonstrates
real Verifier findings invalidating queued authorization through Coordinator.
The [Guard integration contract](docs/guard/guard-integration-contract.md) records
producer/consumer responsibilities, failure handling and proposed approval lookup
semantics, with a pending owner-review checklist for team agreement.

Extend the existing component design and add focused tests and a fixture-driven
example for each increment. Use a feature branch, one reviewer and a focused PR
covering behaviour, validation and affected interfaces.

| Input | Purpose |
| --- | --- |
| [data/sentinel/samples/observations.json](data/sentinel/samples/observations.json) | Healthy, persistent-failure and transient sequences |
| [data/sentinel/samples/messages/](data/sentinel/samples/messages/) | Six wire-message examples validated by [shared/contracts.py](shared/contracts.py) |
| [data/sentinel/samples/baseline.json](data/sentinel/samples/baseline.json) | Exact healthy template and its identity/hash |
| [data/sentinel/samples/policy.json](data/sentinel/samples/policy.json) | Permissions, approval and detection/health thresholds |
| [data/sentinel/samples/dependencies.json](data/sentinel/samples/dependencies.json) / [data/sentinel/samples/health.json](data/sentinel/samples/health.json) | Synthetic graph and health readings |
| [agent/knowledge/](agent/knowledge/) | Curated diagnostic workflows and retrieval guidance |

Keep IDs stable across retries and treat resource versions as opaque strings.
The coordinator and executor own separate records; a local SQLite prototype is
not distributed coordination across hosts. Workload constraints belong in policy,
not hard-coded application logic.

## Observability dashboard

A local, read-only console for following one incident from detection through
Guard validation, execution and verification. Eight views cover Overview,
Kubernetes and each of the six members. It shows CPU/memory trends with fault
markers, node readiness/capacity, Pod health/restarts, Warning events, and an
incident drawer that combines evidence, proposal, Guard findings, execution and
outcome. Expired decisions and stale observations are marked so they do not look
current. Views refresh every two seconds. The React interface uses shadcn/ui,
Tailwind CSS and Recharts.

| Goal | Command |
| --- | --- |
| Demo without a cluster | Double-click `scripts\observability\start-dashboard.bat`, or run `python scripts/observability/dashboard.py --demo` and open **http://127.0.0.1:8765** |
| Watch the local lab live | In `scripts\executor\sentinel-lab.bat`, choose **9** (dashboard + collector windows), then **3** (live test) |
| Inspect a live Executor run | `python scripts/observability/dashboard.py --db data/sentinel/runs/executor-live-<id>/coordinator.sqlite` |
| Connect your own records | `python scripts/observability/dashboard.py` with `scripts/observability/observe_dashboard.py`; see the [dashboard guide](docs/dashboard/dashboard.md) |

The first start builds the frontend with Node.js 20.19+ or 22.12+ and npm; later
starts reuse the build until frontend sources change. Python needs no extra
packages. Keep the launcher's terminal open while using the dashboard.

The dashboard grants no recovery permissions and performs no cluster writes; the
collector uses only `kubectl get`. Demo playback is synthetic and labelled as
such. The Executor view shows records routed through the Coordinator, not the
Executor's own attempt store. The default demonstration uses harness proposals;
offline Planner validation and an optional provider boundary are also present.
The Boutique experiment publishes real Role 6 dependency and health outcomes;
generic/demo playback records retain their producer-specific behavior. The
[dashboard guide](docs/dashboard/dashboard.md) covers live setup, permissions, fault markers,
producer integration and limitations.

## Local lab

On Windows, keep Docker Desktop running and launch `scripts\executor\sentinel-lab.bat`
from PowerShell or by double-clicking it. A persistent terminal provides a menu
for setup, Python tests, live failure/recovery testing, status and cleanup.
Choose **7** to set up the cluster and run both test stages, then **6** when ready
to remove the local cluster and demo image. Choose **8** for focused Guard unit
and integration tests plus the coordinator demo. Choose **9** before **3** to open
the live dashboard and its read-only collector against the lab cluster in separate
windows; the live test records fault and recovery markers on its timeline. The
former `cleanup-sentinel.bat` also opens this menu. Install Docker Desktop, kind, kubectl and Python 3.12+
before using it; missing tools are reported without starting the selected action.

The [Observer live-test guide](docs/observer/observer-live-test.md) records issue #3 commands,
acceptance checks and evidence. The Windows live test saves a healthy revision,
checks the Observer against a healthy workload for at least 60 seconds, injects
the startup fault, verifies threshold timing and 60 seconds of duplicate
suppression, restores the saved revision and checks health again. Recovery is attempted even if the fault
check fails. Option **5** retries recovery; force-closing the terminal can interrupt
recovery, so use that option if needed. Transcripts and JSON evidence are saved in
unique `data/sentinel/runs/windows-lab-*` folders. The cluster remains running until cleanup is
selected. The terminal retains its output until you close it or type `exit` after
leaving the menu. This tests local Kubernetes and uses operator-driven recovery;
it does not deploy the unfinished Sentinel services or create a shared cloud cluster.

With Docker, kind and kubectl available, run from the repo root. Inspect the fault
before restoring it; the fault command's rollout timeout is expected.

```bash
bash scripts/executor/lab.sh up
bash scripts/executor/lab.sh health
bash scripts/executor/lab.sh fault
bash scripts/executor/lab.sh inspect
bash scripts/executor/lab.sh recover
bash scripts/executor/lab.sh health
```

This is an operator-run baseline, not Sentinel's automated executor. Lab evidence
is saved under `data/sentinel/runs/`; copy it before repeating experiments you want to retain.

## Architecture target

One shared repo, with independently deployable services communicating through
versioned events/APIs. Keep service logic and owned data private. Share wire-contract
checks rather than importing another service's business logic. Six ownership areas
do not require exactly six deployed services; agree on cohesive deployment boundaries.

The initial files are development scaffolds. Add runtime and deployment files as
their services become executable, and keep this README current as gates are passed.
