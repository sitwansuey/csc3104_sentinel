# Browser-controlled Boutique experiment

The existing overview can now drive the fixed single-service lab through browser
buttons. This is an opt-in addition to the experiment harness. The ordinary
dashboard remains read-only and the recovery components retain their contracts.

## Run from current source

Start Docker Desktop's Linux engine. With kind, kubectl, Python and the dashboard
build prerequisites installed, run from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Dashboard -UIControls -Cleanup
```

Open **http://127.0.0.1:8765/#overview**. Wait for the healthy baseline and scoped
permission checks to finish. The panel shows the exact cluster/workload and then
enables the next scenario's button:

1. **Inject invalid PORT:** run the ordinary bad-rollout recovery plus existing
   safety checks. Expect a passing Role 6 outcome and RECOVERED incident.
2. **Inject fault + crash Executor:** repeat the fault, terminate the worker after
   its Kubernetes repair write, reconcile on restart, and verify health. Expect
   RECOVERED with zero additional repair writes on restart.
3. **Inject false recommendation:** leave the workload healthy and propose an
   unnecessary rollback. Expect REJECTED with zero Executor patch attempts.

The three scenarios run in order. Each button releases exactly one waiting
harness step. It does not approve a proposal or issue a Kubernetes command from
the HTTP thread. Existing demo approval remains programmatically recorded by
the harness; fresh Guard/Executor validation still controls every repair.

Watch the scenario cards for request-to-outcome elapsed time, and use **Inspect
incident** for actual role evidence. The existing overview, role tabs, cluster
timeline and incident drawer remain available. A request acknowledgement is not
evidence that injection or recovery succeeded. Only recorded scenario outcomes
and the corresponding role records establish the result.

After inspection, Ctrl+C in the launcher and let cleanup finish. `-Cleanup`
removes only `sentinel-boutique-lab`. The run's evidence and image caches remain.
For an interrupted wrapper, remove only that named lab with:

```powershell
kind delete cluster --name sentinel-boutique-lab
```

An optional second terminal can watch Pods:

```powershell
kubectl --context kind-sentinel-boutique-lab -n sentinel-boutique get pods -w
```

Use the browser buttons rather than independently changing the workload during
an active scenario. This remains a guided experiment, not a persistent general
controller. To repeat all scenarios, start a fresh session after cleanup. Avoid
leaving the measured baseline idle for a long time: existing freshness policies
remain active and can refuse an expired baseline or decision.

## Packaged route

Build the runner from this source, then use:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -UIControls
```

The launcher creates `sentinel-boutique-package`, publishes the UI only on host
loopback, and removes the named runner/cluster after Ctrl+C. `-UIControls`
replaces terminal Enter prompts; it cannot be combined with `-Unattended`.
`-GuardRejection -UIControls` runs just the final healthy-workload rejection.

**The previously exported 0.3.0 TAR predates this UI feature.** Do not use
`-SkipBuild`/`-Offline` with that old image expecting the new flag. The earlier
zero-CVE result belongs to its recorded digest; rebuild, rescan and rehearse any
new image before sharing. The documented `-PauseBeforeInjection` terminal route
and ordinary read-only dashboard still work without these controls.

## Implementation and boundaries

| File | Responsibility |
| --- | --- |
| `runtime/lab_control.py` | Fixed lab scope, ordered gates, duplicate-click handling and token-free audit state |
| `runtime/dashboard.py` | Read-only by default; explicit local capability and injection endpoints when a controller is supplied |
| `scripts/test_boutique_live.py` | Wait at each gate, perform the existing injections and mark measured recovery outcomes |
| `scripts/guard_rejection_demo.py` | Gate the existing unnecessary-rollback test and publish its measured refusal |
| `scripts/boutique_dashboard.py` | Own the API/collector lifetime and stop waiting controls on teardown |
| `dashboard/src/components/lab-experiment.tsx` | Visible scenario controls, timing and incident links on the overview |

The API accepts only the current session/gate IDs. It does not accept targets,
commands, templates, approvals or action parameters. Requests require an
ephemeral session token, a matching local Origin/Host and bounded JSON. Foreign
hosts/origins and stale gates are refused. There is no CORS authorization. This
is a trusted local lab interface, not a multi-user authenticated control plane.

Scenario state is saved to `lab-session.json` in the run folder without the
control token. This is audit evidence, not a durable mutation queue: process
restart does not automatically resume an injection. The Executor's own durable
reconciliation behavior is unchanged. Interrupted work remains subject to the
existing operator emergency-restore and cleanup behavior; an emergency restore
does not count as Executor success.

## Evaluation handoff to Planner

Online Boutique is the application workload, not the name of a single standard
dataset. Its official repository provides a connected storefront and load
generator: [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo).
Confirm the exact published dataset's name, version, license, telemetry fields,
fault labels and train/test split before choosing Planner evaluation tasks.

Keep two evaluations distinct:

- **Offline Planner benchmark:** diagnose the recorded root cause from permitted
  pre-repair evidence. Measure root-cause accuracy, valid structured proposals,
  supported target/action selection, latency and token/cost usage where relevant.
  Use a held-out split; do not place test labels or expected repairs in retrieval
  inputs. A published diagnosis dataset may have no recovery-action ground truth.
- **Live Sentinel benchmark:** inject a known fault into a versioned workload,
  collect incident evidence, obtain a real Planner proposal, run Guard/approval
  and restricted execution, and measure independent recovery outcomes and unsafe
  write prevention. Offline accuracy alone does not establish live recovery.

For live cases, retain the run ID, workload/image versions, actual fault time,
target, injection configuration, ground truth, diagnosis/proposal, Guard result,
repair-write counts, execution result and Verifier window/outcome. Ground-truth
scenario labels must remain separate from the Planner's evidence inputs.

The panel's request-to-outcome timer includes the harness's safety checks and is
not a research MTTR measurement. Use actual fault/detection/execution/outcome
timestamps from the recorded evidence for a defined recovery metric.

Next sequence: this repeatable UI experiment → real Planner adapter for the
existing action contract → connected Boutique traffic/dependency evaluation →
additional fault types and Chaos Mesh. A persistent network fault cannot be
assumed repairable by a Pod-template rollback. Define the fault lifecycle and
appropriate guarded action before adding such scenarios. Simultaneous faults
and security remediation should follow reproducible single-fault experiments.

## Recorded validation: 4 November 2026

- Six shared contract fixtures and **698 Python tests** passed offline. The new
  tests cover fixed scope, setup/stale sessions, concurrent duplicate requests,
  next-gate exclusion, audit failure, shutdown, outcome requirements, default
  read-only behavior, and HTTP origin/token/body checks.
- Frontend type-check/build and **72 component tests** passed. The existing
  non-failing bundle-size warning remains. PowerShell parsing and whitespace
  checks passed.
- Actual source run `runs/boutique-live-20261103T173128Z-814440/` completed all
  **13 real Kubernetes checkpoints**. Its identifier is UTC; the run occurred on
  4 November in Singapore. Installed Edge clicked all three live buttons; each
  returned HTTP 202 with zero page JavaScript errors. Desktop/mobile ready-state
  screenshots were reviewed and the mobile page had no horizontal overflow.
- `lab-session.json` finished COMPLETED with RECOVERED, RECOVERED and REJECTED.
  The restart used one read and zero write attempts. Dashboard assertions checked
  two APPLIED actions, two passing Role 6 outcomes, three dependency results,
  seven Guard reports and three incidents. The named lab and private Executor
  credentials were removed; evidence and image caches remain.
- The final completed-results layout and incident drawer were separately checked
  using a **read-only replay of that run's real evidence**, not a second live
  recovery. Browser QA helpers and screenshots are under ignored
  `runs/lab-ui-validation/`. The temporary replay server was stopped afterward.

No new image was built/scanned for this UI change. The previous exported image
and its historical CVE scan remain separate artifacts. Planner, connected-service
recovery, Chaos Mesh and a named published dataset were not integrated by this
change. The benchmark source still needs confirmation from the Planner owner.
