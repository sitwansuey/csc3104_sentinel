# Local Boutique demo: setup, injection, recovery and cleanup

The canonical current procedure is [the connected Boutique guide](boutique-connected.md).
It deploys the storefront, checkout and supporting services: eleven Deployments
in total. Fault injection and restricted repair currently target shipping only.
Roles 1, 2, 4, 5 and 6 run their actual implementations through the local harness.
The harness supplies the recovery proposal and records a demo approval; no LLM
call or authenticated human approval portal is claimed.

## 1. Prepare the host

Install Docker Desktop (Linux engine), kind, kubectl, Python 3.12+ and Node/npm
for the dashboard build. Start Docker Desktop. From the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1 -CheckOnly
```

If tools are missing, use the installer without `-CheckOnly`, or follow the
[manual dependency instructions](dependencies.md). Docker Desktop's separate
built-in Kubernetes cluster is unnecessary; this experiment creates its own kind
cluster. Full Boutique needs sufficient Docker memory/CPU for all eleven Pods.

## 2. Start the connected experiment

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
```

- Sentinel: http://127.0.0.1:8765/#overview
- Boutique: http://127.0.0.1:8080

Wait for the measured healthy baseline and permission checks. Select
**Online Boutique** for service readiness, shopping measurements, namespace Pods,
restarts and warning events. Use the role pages and incident drawer for Sentinel
decisions, approvals, execution and formal verification outcomes.

## 3. Run the three ordered UI scenarios

1. **Inject invalid PORT**: actual startup failure, Observer detection, Guard
   checks, restricted template restore and independent Verifier recovery.
2. **Inject fault + crash Executor**: actual repair followed by process exit;
   restart reconciles the committed write without another repair mutation.
3. **Inject false recommendation**: propose a rollback while already healthy;
   Guard refuses it and the Executor makes zero repair attempts.

The buttons release fixed harness steps. They do not approve a recovery or run
arbitrary Kubernetes commands from the HTTP handler. The harness's demo approval
cannot bypass hard Guard constraints. APPLIED remains VERIFYING until Role 6
publishes a sustained outcome. Cart/checkout must recover as well as shipping.

For automatic execution, replace `-UIControls` with `-ExitAfterTest`.
Omit `-Connected` to use the smaller shipping-only experiment. Do not claim that
the standalone `test_executor_live.py` HTTP lab provides the same role coverage.

## 4. Observe and inspect evidence

```powershell
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get pods -w
```

```powershell
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected logs deployment/checkoutservice --follow --tail=50
```

The run directory printed at startup contains Coordinator/Observer/Executor/
Verifier journals, proposals, Guard reports, approvals, actual request results,
outcomes and cleanup evidence. The dashboard projects these records without
creating another incident stream. Shopping requests are measured at test stages;
the UI is not an all-request trace or application-log streaming console. Usage
metrics remain unavailable unless the Metrics API supplies actual measurements.

## Code used at each stage

| Stage | Main files |
| --- | --- |
| Host checks and cluster ownership | `scripts/install-dependencies.ps1`, `scripts/test-boutique-live.ps1`, `infra/kind.yaml` |
| Connected workload and shopping probes | `scripts/boutique_connected.py`, `scripts/boutique_storefront.py`, pinned `infra/boutique-connected-upstream.yaml` |
| Harness proposal, injection and scenarios | `scripts/test_executor_live.py`, `scripts/test_boutique_live.py`, `runtime/lab_control.py`, `scripts/guard_rejection_demo.py` |
| Observation and incident delivery | `services/observer.py`, `services/observer_kubernetes.py`, `runtime/observer_pipeline.py`, `services/coordinator.py`, `services/coordinator_store.py` |
| Dependency findings and authorization | `services/verifier.py`, `runtime/guard_pipeline.py`, `services/guard.py` |
| Conditional repair and reconciliation | `runtime/live_executor.py`, `runtime/executor_pipeline.py`, `services/executor.py`, `services/executor_store.py`, `services/executor_delivery_store.py`, `services/k8s_executor_adapter.py` |
| Independent outcome | `runtime/verifier_pipeline.py`, `runtime/verifier_durable.py`, `services/verifier_store.py` |
| UI and operational projections | `scripts/boutique_dashboard.py`, `runtime/dashboard.py`, `services/coordinator_read.py`, `runtime/cluster_metrics.py`, `runtime/boutique_observability.py`, `dashboard/src/` |

## 5. Clean up

After inspection, press Ctrl+C in the launcher and let teardown finish. The
`-Cleanup` flag removes its named cluster; the harness stops the dashboard and
port-forward and removes private Executor credentials. Evidence and cached
images remain. With `-ExitAfterTest`, teardown happens automatically.

For interrupted cleanup, inspect and remove only the dedicated lab:

```powershell
kind delete cluster --name sentinel-boutique-connected
kind get clusters
```

## Other modes

- [Security lab](security-connected.md): separate opt-in mTLS/inspection/CVE
  experiment. It does not yet run the guarded recovery pipeline; issue #69 tracks
  that integration.
- [Provider integration](provider-recovery.md): optional trusted provider adapter
  boundary. Offline mock planning exists; actual LLM integration remains pending.
- [Packaged runner](packaged-demo.md): the exported 0.3.0 image is historical and
  lacks these latest connected/UI changes. Use source for the current system.

See [the consolidation review](repository-review-2026-11-05.md) for exact tests,
branch decisions, issue status and remaining limitations.
