# Sentinel observability dashboard

Follow detection, planning, Guard validation, execution and verification in one
local workspace. The React interface uses shadcn/ui components, Tailwind CSS and
Recharts. The dashboard polls every two seconds, supports incident and
workload filters, and has ten views:

| View | Records shown |
| --- | --- |
| Overview | Cluster CPU/memory trends, node/Pod readiness, pipeline, lifecycle events and fault markers |
| Kubernetes | Node capacity/readiness, namespace Pod health/restarts, measured CPU/memory and Warning events |
| Online Boutique | All eleven Deployment readiness cards, stage-based shopping results, namespace Pods/restarts and warning events |
| Security | Inspection metadata and image scan/alert evidence when the selected journal has security producers |
| Member 1: Coordinator | Incident register, pending/acknowledged routes, recorded approvals |
| Member 2: Monitoring | Ready/desired/available replicas, detection evidence, collector errors |
| Member 3: Planner / RCA | Proposed action, target/version, rationale, baseline parameters, evidence references |
| Member 4: Recovery Guard | Decision history, reason codes, expiry, exact input binding, individual PASS/FAIL/UNKNOWN/SKIP findings |
| Member 5: Executor | Reported attempts, conflicts, reconciliation results, pending execution routes |
| Member 6: Dependencies / Health | Pre-execution dependency status and post-execution health checks/windows |

The default dashboard is read-only. The dedicated Boutique harness can opt in
to [fixed browser injection gates](experiment-controls.md); those gates do not
approve proposals or run repair commands from HTTP handlers. Role implementations
still authorize/execute/verify the resulting recovery. No cloud deployment or
shared-cluster acceptance is claimed. Application logs are inspected with
`kubectl logs`; this UI does not stream every request or application log.

## Try it now without a cluster

On Windows, double-click **`scripts/start-dashboard.bat`**. It locates the
repository, checks for Python 3.12+, builds the frontend when needed, chooses an available port and opens your
default browser. Keep its terminal open while using the dashboard. Errors stay
visible if startup fails. The launcher always starts the isolated demo.

For the first build, install **Node.js 20.19+ or 22.12+ with npm** as well as
Python 3.12+. A current Node.js LTS release works. Reopen your terminal after
installing Node so the launcher can find it. The first run downloads frontend
dependencies; subsequent runs reuse the build until the frontend changes.

From the repository root:

```powershell
python scripts/dashboard.py --demo
```

Open **http://127.0.0.1:8765**. No external Python packages are needed. The command
automatically installs frontend dependencies and builds the interface when
missing or changed. A current built frontend can run without Node. Stop with
Ctrl+C; use `--port 8766` if needed.
You can also use `--port 0` to choose an available port and `--open-browser` to
open the printed URL automatically:

```powershell
python scripts/dashboard.py --demo --open-browser --port 0
```

If you prefer to build explicitly, run these commands from the repository root:

```powershell
npm --prefix dashboard ci
npm --prefix dashboard run build
python scripts/dashboard.py --demo
```

Only the generated `dashboard/dist/index.html` and `/assets/` files are served.
Frontend source, npm configuration and `node_modules` are not public routes.
If a server is started directly without a build, its home page returns an
actionable build message with HTTP 503.

For frontend development, leave the Python server on port 8765 in one terminal,
then use Vite's development server in another:

```powershell
npm --prefix dashboard run dev
```

Open the URL printed by Vite. Its `/api` proxy reads the Python backend at
`http://127.0.0.1:8765`. Use the Python server's printed URL for the production
build. Both modes use the same read-only snapshot API.

The conspicuous **DEMO** banner stays visible and explicitly labels the measurements synthetic. A 36-second loop
shows a healthy workload, injected-failure marker, detection, mock proposal,
dependency finding, approval escalation, synthetic operator approval, Guard
ALLOW, mock execution and mock verified recovery. The Observer and Guard really
evaluate fixtures. Planner, Executor, Verifier, approval, baseline and health
evidence are synthetic. The demo never contacts Kubernetes, opens the configured
live databases, or authorizes a real write. It uses an isolated temporary
database, precomputes frames, then replays them. Message times retain the fixture
clock; telemetry markers are created when the demonstration is prepared. Cluster
charts, three worker nodes and eight Pods are fabricated to demonstrate the
interface; they do not represent an installed cluster.

## Connect the actual records

In terminal 1:

```powershell
python scripts/dashboard.py --db runs/coordinator.sqlite3 --telemetry runs/telemetry.sqlite3
```

The server binds only to **127.0.0.1**. Missing databases are reported as
disconnected; reading never creates or migrates them. Both SQLite stores must be
on the same host as their writers. A working database connection means records
are readable; it does not prove that all producer processes are alive.

In terminal 2, if your team has Kubernetes running:

```powershell
python scripts/observe_dashboard.py `
  --context kind-sentinel-lab `
  --cluster-id sentinel-lab `
  --namespace sentinel-demo `
  --policy samples/policy.json `
  --db runs/coordinator.sqlite3 `
  --telemetry runs/telemetry.sqlite3
```

Use your actual context, cluster ID, namespace and reviewed policy. For Online
Boutique, select its namespace. The sample lab names above refer to the existing
local demo, not an installed Online Boutique instance. The Observer currently
uses `policy.detection.failure_duration_seconds` (30 seconds in the sample).
The collector observes all Deployments in the selected namespace; the Guard's
separate target/action allowlist still controls recovery eligibility.

After each polling cycle, the collector waits three seconds by default. Each cycle:

1. Uses an explicit context and `kubectl get deployments` with bounded timeouts.
2. Converts each Deployment using Member 2's existing adapter.
3. Saves only replica/generation metrics and target identity to the observation
   journal, without Pod templates, Secret values or raw logs.
4. Runs the existing Observer and submits detected incidents to the Coordinator.
5. Saves collector heartbeats/errors so failures in monitoring are visible too.
6. Independently collects nodes, namespace Pods, Warning events, and node/Pod
   Metrics API readings. These five sources run concurrently with bounded
   timeouts. Cluster collection still runs if Deployment observation fails.

Use `--interval 5` to adjust the delay (1–60 seconds), or `--once` for one cycle.
The dashboard marks an observation/event older than 15 seconds as stale. This
is a UI liveness indicator, independent of Guard policy freshness; intervals
above 15 seconds can show stale between successful polls. Observation history
is local to this process and resets on collector restart; production deduplication
and service supervision still need integration. Detected incidents queue planning
requests; this command does not run the unfinished Planner or recovery pipeline.

## Kubernetes metrics and permissions

The **Kubernetes** tab reads five independent sources:

| Source | Display | Read permission needed |
| --- | --- | --- |
| Nodes | Ready condition, pressure conditions, capacity and allocatable CPU/memory | List nodes at cluster scope |
| Pods | Phase, readiness condition, container readiness, restarts and failure reasons | List Pods in the selected namespace |
| Events | Warning reasons, object identity, count and last observed time | List events in the selected namespace |
| Node Metrics API | Measured node CPU and memory | List nodes in `metrics.k8s.io` |
| Pod Metrics API | Summed container CPU/memory per Pod | List Pods in `metrics.k8s.io` in the namespace |

The configured context must already have these permissions. The collector does
not change RBAC, install Metrics Server, modify kubeconfig or create a cluster.
Unavailable sources show a sanitized status such as permission denied, API
unavailable or timeout. You can still inspect Pod health if cluster-wide node
access or resource usage access is unavailable.

CPU usage is the average number of cores during the Metrics API sampling window.
Pod CPU uses millicores (`1000m = 1 core`). Memory usage is the reported working
set in bytes, displayed as MiB/GiB. These are **actual measurements**, distinct
from requests/limits and allocatable capacity. Overview percentages use measured
**node** usage divided by **node allocatable** capacity across the cluster.
Pod inventory covers only the chosen namespace. It is not divided by unrelated
cluster-wide capacity to suggest a namespace utilization percentage.

The Metrics API must be installed and reachable for usage measurements. Node
and Pod status are still useful without it. Missing or malformed usage stays
`null`, not zero. Incomplete measurements produce `PARTIAL`; incomplete totals
are unavailable. The UI retains per-node/Pod measurement timestamps and windows,
so recently fetched but old measurements can be recognized. Readiness is based
on the Kubernetes Ready condition; all containers being ready is displayed
separately and does not override Pod readiness gates. A displayed OOM/failure
reason may come from a container's previous termination; inspect the workload
to distinguish an active failure from its history.

Inventory is bounded to 100 nodes, 300 Pods and 100 Warning events, with explicit
truncation metadata. Latest 60 cluster snapshots are exposed for charts; at most
600 are retained locally. Bounds apply globally to the configured journal, not
separately per cluster. Lists may omit resources beyond those limits. Usage
charts space samples by collection order and leave gaps for missing data.

See the official [Kubernetes resource metrics pipeline](https://kubernetes.io/docs/tasks/debug/debug-cluster/resource-metrics-pipeline/)
for measurement semantics and the [node status reference](https://kubernetes.io/docs/reference/node/node-status/)
for capacity and conditions.

## Observe an injection or real error

Start the collector and dashboard before running the team's existing fault
injection procedure. Replica degradation and detected incidents then appear as
the Kubernetes state changes. The dashboard does not automatically identify
whether a failure was injected or accidental.

Record an injection marker alongside the team's fault procedure:

```powershell
python scripts/dashboard_event.py --member 1 --type fault `
  --summary "Starting the controlled startup-failure experiment"
```

Run the actual existing fault command separately. If it succeeds or fails,
record that result with another `status` or `error` marker. The marker command
**only records text**; it does not cause or prove an injection. Optional
`--incident-id` and `--action-id` associate a marker with existing records.
Unscoped markers remain visible under incident filters and are labelled
`System / unscoped`. All error/fault markers appear on Overview; each member tab
also shows its own component's error/fault markers.

For the existing local lab, the fault/recover controls remain in
`scripts/sentinel-lab.bat` or `scripts/lab.sh`; do not treat a dashboard marker as
a replacement for their outcome checks. On Windows, menu option **9** of
`sentinel-lab.bat` starts this dashboard and the collector above for
`kind-sentinel-lab` in two windows. Option **3** then records Member 1 markers
when it injects the fault, detects it, restores the saved revision, and finishes
or fails; marker failures are reported but never fail the test. `lab.sh` does not
start the dashboard. No injection was run during dashboard development.

## How each teammate connects

All six tabs read the agreed contracts already stored by the Coordinator. Each
producer uses its existing route into `handle(message, store, bus)` or the CLI:

```powershell
python scripts/coordinator.py --db runs/coordinator.sqlite3 ingest path/to/message.json
```

Messages must satisfy lifecycle ordering and use the matching incident, action
and exact target identity. Opening the dashboard does not consume pending routes.

| Member | Integration |
| --- | --- |
| 1 | Keep the Coordinator running against the selected database. Existing approval handling remains outside the dashboard. |
| 2 | Use the collector above, or an existing adapter feeding contract-valid `incident` messages. |
| 3 | Consume `planner.request`, then submit a `proposal`. Offline runbook/proposal validation and an optional provider boundary exist; the default live demo uses explicit harness proposals and makes no actual LLM call. |
| 4 | Run the existing Coordinator Guard worker. New evaluations atomically persist the bound detailed report, decision, downstream route and input acknowledgment. |
| 5 | Consume execution routes with the required fresh Guard checks, then submit `execution_result`. A pending route is not counted as an attempted write. |
| 6 | Submit pre-execution `dependency_result` and post-execution `outcome`. Boutique invokes the real dependency evaluator and durable post-execution worker using measured gRPC, readiness and template evidence; absent findings remain unknown. Opening this read-only dashboard does not run those workers. |

Member 4 example with recorded inputs:

```powershell
python scripts/coordinator.py --db runs/coordinator.sqlite3 `
  --telemetry runs/telemetry.sqlite3 guard `
  --policy path/to/policy.json --baseline path/to/baseline.json `
  --state path/to/current-state.json
```

The worker uses stored dependency results when `--dependencies` is omitted. It
processes currently pending requests once; keep using the existing service/CLI
workflow for subsequent requests. Standalone Guard CLI reports and historical
results ingested from other producers are not automatically imported into the
new `guard_reports` table. Such decisions remain visible, with an explicit
missing-detailed-report message. Existing databases gain the additive table when
the normal Coordinator writer next opens them; the dashboard reader never
migrates them.

The optional Coordinator `--telemetry` flag journals command/worker failures with
their component number and exception type. It does not publish arbitrary stderr
or change the original failure/rollback behavior. An unavailable journal cannot
mask the original command failure.

Any member can publish an optional component error or heartbeat:

```python
from runtime.telemetry import emit_event

emit_event(
    "runs/telemetry.sqlite3",
    component=3,
    event_type="error",
    summary="Planner provider timed out; no recovery proposal produced",
    incident_id="INC-EXAMPLE",
)
```

Supported journal types are `observation`, `error`, `heartbeat`, `fault` and
`status`. Replica metrics have a small allowlist. Do not include credentials,
Secret values or raw sensitive telemetry in summaries or contract messages.
The observation journal is never trusted evidence for Guard authorization.

## Reading the displays correctly

- Every two seconds the browser fetches a read-only snapshot. A disconnected
  server leaves a visible warning; pause freezes the display until resumed.
  Manual refresh works while paused.
- Each database is read in its own consistent SQLite transaction. The two stores
  do not share an atomic snapshot; an observation can appear just before its
  incident. Pending writes appear on a following refresh.
- Views cover at most the latest 600 rows **per table**, not unlimited history.
  Filters apply within that window. Old messages, reports or chart samples may
  be absent even while a current incident is visible.
- Selecting an incident scopes workload observations to its exact target,
  including UID. Unscoped system/fault/error markers remain explicitly labelled.
- Lifecycle activity uses database insertion order. Proposals have no producer
  timestamp in the current contract, so the dashboard says so instead of
  inventing a time. Outbox acknowledgment is delivery bookkeeping, not proof of
  a recovery effect. Outbox entries without an action ID are unscoped.
- `RECOVERED` means a stored outcome says recovered, not a fresh health probe.
  The readiness chart shows one workload's recorded samples, selected by the
  workload filter or the latest observed workload. It does not aggregate
  unrelated services or prove sustained application health.
- No p95 latency, request error rate, distributed traces or
  inferred dependency edges are shown without a corresponding producer. These
  need future OpenTelemetry/Prometheus adapters and additional agreed contracts.
- The local server is not an authenticated shared team gateway. For a shared
  deployment, Member 1 must provide hosting, authenticated access and a central
  event/API integration. Do not distribute a SQLite file across laptops as a
  replacement for a central service.

## Validation

```powershell
npm --prefix dashboard test
npm --prefix dashboard run build
python -m unittest discover -s tests -p "test_dashboard.py" -v
python -m unittest discover -s tests -p "test_*.py"
python scripts/check_contracts.py
```

Build the frontend before running the Python HTTP checks:

```powershell
npm --prefix dashboard ci
npm --prefix dashboard run build
```

Tests cover read-only snapshots, absent/older databases, bounded history,
persisted Guard reports and transaction rollback, isolated playback, Kubernetes
read-only collection/incident routing, built frontend HTTP routes and refused
mutations. Frontend checks cover the React views and safe rendering of stored
strings. Automated component checks do not replace real browser and responsive
layout review.

The frontend uses the official shadcn/ui registry components in
`dashboard/src/components/ui/`, configured by `dashboard/components.json`.
The React views use those Cards, Tabs, Selects, Tables, Alerts, Progress bars,
Tooltips and Charts directly. The Metrics API collector and Guard evaluation
remain Python components. See the [official Vite installation guide](https://ui.shadcn.com/docs/installation/vite)
for the frontend setup. Add components from `dashboard/` with
`npx shadcn@latest add <component>`.
