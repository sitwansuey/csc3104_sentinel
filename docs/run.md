# Run, inject, observe and clean up

Complete [setup](setup.md) first. This is the canonical connected-source flow.
Eleven Boutique Deployments run, while supported recovery targets shipping.
The harness/model supplies a proposal; Guard and restricted Executor remain
mandatory, and Verifier determines the formal outcome.

## Launch and inject

Windows PowerShell:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
```

Linux/macOS, subject to the architecture limitations in setup:

```bash
bash scripts/run-sentinel.sh --setup --cleanup
```

Open Sentinel at **http://127.0.0.1:8765/#overview** and Boutique at
**http://127.0.0.1:8080**. Wait for the measured healthy baseline and permission
checks. Use the three ordered buttons on Overview:

1. **Inject invalid PORT:** shipping's new containers cannot start; cart/checkout
   requests fail. Observer detects sustained failure; a harness proposal goes
   through Guard, trusted demo approval, restricted execution and Verifier.
2. **Inject fault + crash Executor:** a real patch succeeds before the worker
   exits. Restart reconciles it from live state/action evidence without another
   repair write, then Verifier measures sustained recovery.
3. **Inject false recommendation:** the service is healthy and already matches
   its baseline. Guard rejects the unnecessary rollback with zero repair writes.

The buttons release fixed experiment steps; they do not approve an action or
run arbitrary Kubernetes commands. Demo approval is pre-authorized local lab
approval, not an authenticated human approval portal. Human approval never
bypasses hard Guard constraints. An APPLIED result leaves the incident VERIFYING
until the independent outcome arrives.

For unattended execution and automatic cleanup:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Cleanup
```

The shell equivalent is `bash scripts/run-sentinel.sh --setup --cleanup --connected --dashboard --exit-after-test`.
Omit `-Connected` on Windows for the smaller shipping-only lab. Use
`-StorefrontPort 8081` or `-DashboardPort 8766` if a port is occupied.

For terminal-controlled injection replace `-UIControls` with
`-PauseBeforeInjection`. If injecting manually, wait at the first healthy
baseline gate, then run:

```powershell
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected set env deployment/shippingservice --containers=server PORT=invalid-port
```

Then release the first terminal gate. The harness reasserts the same fault and
starts detection/recovery. It is paused until that point; it is not an always-on
production controller. Let subsequent scenarios inject themselves and avoid
changing configuration during recovery. [Planner mode](planner.md) replaces the
proposal source and runs one agent case; [Chaos Mesh](chaos.md) is separate.

## Observe Sentinel and Boutique

**Online Boutique** shows all eleven Deployment readiness cards, stage-measured
homepage/cart/checkout results, Pod readiness/restarts and Kubernetes warning
events. Role pages and the incident drawer show decisions, approvals, attempts,
results and formal outcomes. Updates poll every two seconds; slow collection
can extend the interval. Stale/disconnected/unknown data is explicit. CPU/memory
usage is unavailable unless an actual metrics source supplies it.

Watch Pods or follow one service in a second terminal:

```powershell
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get pods -w
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected logs deployment/checkoutservice --follow --tail=50
```

Run those commands separately. Shopping requests are measured at experiment
stages, not every public request. The dashboard does not stream all application
logs or complete distributed traces. Dependency topology is configured from the
reviewed manifest, not automatically discovered. Sentinel workers run on the
host; their activity is visible through their journals, not six Kubernetes Pods.

The dashboard's `GET /api/snapshot` is a bounded read projection. The default
server has no mutation routes. Only `-UIControls` enables the separate guarded
lab-gate routes. The browser receives no kubeconfig or database paths.

## What code runs

| Stage | Code |
| --- | --- |
| Tools and cluster | `scripts/install-dependencies.ps1`, `scripts/test-boutique-live.ps1` or `scripts/run-sentinel.sh`, `deploy/kind/kind.yaml` |
| Workload, baseline and injection | `experiments/boutique/harness/live.py`, `connected.py`, `deploy/boutique/upstream.yaml` |
| Observation and lifecycle | `services/observer/`, `runtime/observer_pipeline.py`, `services/coordinator/` |
| Planning | `experiments/recovery/live.py` for harness proposals; `experiments/boutique/harness/agent_live.py`, `agent/`, `services/planner/` and `services/rca/` for agent mode |
| Guard and execution | `runtime/guard_pipeline.py`, `services/guard/`, `runtime/executor_pipeline.py`, `services/executor/` |
| Formal outcome | `runtime/verifier_pipeline.py`, `runtime/verifier_durable.py`, `services/verifier/` |
| UI projection | `experiments/boutique/harness/dashboard.py`, `runtime/dashboard.py`, `runtime/boutique_observability.py`, `dashboard/src/` |

## Evidence and cleanup

The printed run directory (normally under `data/experimentation/runs/`) contains
Coordinator/Observer/Executor/Verifier journals, the baseline, proposal/approval,
Guard reports, application measurements, dashboard snapshots, outcome and
cleanup evidence. `summary.json` records actual checks. Preserve sanitised
evidence separately; output directories and credentials are not source files.

With `-Cleanup`, Ctrl+C tears down the dedicated cluster after stopping the
dashboard and port forward and removing private Executor credentials.
`-ExitAfterTest` does that automatically. Let teardown finish instead of force
closing the terminal. For interrupted cleanup:

```powershell
kind delete cluster --name sentinel-boutique-connected
kind get clusters
```

Stop any watcher terminal separately. For shipping-only runs the cluster is
`sentinel-boutique-lab`. Do not delete other clusters, globally prune Docker or
count administrator emergency restoration as successful Sentinel execution.
Docker Desktop, evidence and cached images remain after normal cleanup.
