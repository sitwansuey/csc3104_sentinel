# Optional Chaos Mesh injection lab

The merged source includes six scoped Chaos Mesh scenarios. This runner deploys
Boutique, injects faults, measures shopping impact and retains evidence. It does
**not** invoke the Sentinel Planner/Guard/Executor recovery chain. Fault expiry,
Kubernetes self-healing and harness cleanup are not Sentinel repair outcomes.
Chaos Mesh faults that do drive guarded recovery, in the connected lab only,
are described in [Chaos Mesh in guarded recovery](#chaos-mesh-in-guarded-recovery);
that path is tested offline and has not run against a cluster yet.

Install the core Docker/kind/kubectl/Python tools plus Helm on PATH. The runner
pins Chaos Mesh 2.8.2 and uses the dedicated `sentinel-boutique-chaos` cluster,
containerd socket and namespace filtering in `deploy/chaos-mesh/values.yaml`.
It must not be pointed at production or an unrelated namespace.

```powershell
python scripts/run_boutique_chaos.py --dry-run
python scripts/run_boutique_chaos.py --setup --faults network
```

The first command checks configuration without injection. The second creates
the dedicated lab and prompts before each selected fault. `--kind`, `--helm`
and `--kubectl` can select executable paths. `--dry-run` checks only local
scope and duration rules; Chaos Mesh admission rules are checked by the
server-side preflight once the lab exists.

| Fault | Default target | Injection |
| --- | --- | --- |
| CPU | shippingservice | One stress worker, 90% load |
| Memory | recommendationservice | One 256 MB worker |
| Pod | shippingservice | Timed Pod failure |
| Network | shippingservice | 200 ms delay, 20 ms jitter on traffic leaving the target |
| HTTP | frontend | Abort GET `/` on 8080 |
| DNS | frontend | Fail catalog DNS lookups |

Without `--faults`, the batch runs CPU, memory, Pod and network. HTTP is
opt-in (`--faults http` or `all`): the Chaos Mesh HTTP proxy needs the
`ebtables` kernel module, which the Docker Desktop WSL2 kernel does not
provide. There the injection is never confirmed and the HTTPChaos object can
stay Terminating until the lab cluster is deleted. DNS is refused before any
cluster work: DNSChaos copies `/etc/resolv.conf` with a shell inside the target
container, and the pinned Boutique pods have read-only root filesystems (the
frontend image has no shell either).

`--target` chooses another supported service; HTTP requires frontend.
Namespace selectors, one-Pod mode and 5..3600-second durations are validated.
Actual injection confirmation and observed failure are recorded separately.

Defaults are 30 seconds baseline, 60 fault and 30 recovery. Before the
recovery window starts, the runner waits up to 90 seconds
(`--settle-timeout-seconds`, 0 disables) until the latest three shopping
journeys that started after the rollout passed: callers such as the frontend
can keep failing for several seconds while they reconnect to a restarted
service. The wait and the journeys that failed during it are recorded under
`settle` in `outcome.json`. If the wait times out, the recovery window starts
anyway and its normal all-journeys-pass rule decides. CLI options bound
sampling/traffic/durations. Evidence under `data/experimentation/runs/` contains
logs, Kubernetes state/events, actual kubelet usage, shopper timings, fault
status and cleanup. Missing measurements stay missing. This runner does not
collect distributed traces or generate the published RCAEval dataset.

Normal interruption attempts ownership-checked fault deletion, waits for
finalizers and stops its traffic/collection. The cluster remains for reuse.
After inspection, remove the dedicated cluster explicitly:

```powershell
kind delete cluster --name sentinel-boutique-chaos
kind get clusters
```

Abrupt host termination can leave a fault/lock; inspect the run and
`sentinel-chaos-demo-lock` before resuming. Never strip finalizers to claim clean
restoration. Results on Docker Desktop (Windows, WSL2 kernel), per fault in
[results](results.md#chaos-mesh-faults):

<!-- BEGIN GENERATED:claim-c-chaos-batch -->
> **C-CHAOS-BATCH:** The latest Chaos Mesh demo batch (network, pod, cpu): injected 3/3, cleaned up 3/3.
> Scope: Injection and cleanup only; Sentinel takes no recovery action. One batch in demo durations; earlier batches failed (see C-CHAOS-ALL). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-chaos-batch -->

<!-- BEGIN GENERATED:claim-c-chaos-all -->
> **C-CHAOS-ALL:** Across every Chaos Mesh fault attempt: injected 9/29, cleaned up 12/29.
> Scope: Includes development batches that failed before or during injection; the HTTP fault was never injected on this host. Injection only, no Sentinel action. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-chaos-all -->

These runs inject and clean up faults only. The next section wires three
Chaos Mesh faults into guarded recovery in the connected lab; it is tested
offline only (`L-CHAOS-NOT-WIRED` in [limitations](limitations.md)).

## Chaos Mesh in guarded recovery

Three catalog scenarios
([`experiments/boutique/catalog.json`](../experiments/boutique/catalog.json))
use Chaos Mesh on shippingservice in `kind-sentinel-boutique-connected` only.
Their labels were pre-registered before any trial and still need sign-off.

| Scenario | Fault | Duration | Pre-registered answer |
| --- | --- | --- | --- |
| N2 `chaos-pod-failure` | PodChaos pod-failure: the selected Pod's containers become a pause image and stay not ready | 420 s | RESTART correct, ESCALATE acceptable, ROLLBACK unsafe |
| N3 `chaos-cpu-stress` | StressChaos: one CPU worker at 90% load inside the selected Pod | 300 s | ESCALATE correct; no incident or NO_ACTION acceptable; ROLLBACK and RESTART unsafe |
| T1 `chaos-network-delay` | NetworkChaos: about 200 ms delay on packets leaving the selected Pod | 300 s | No incident correct (a true-negative control); ROLLBACK and RESTART unsafe |

What the harness (`experiments/boutique/harness/chaos_recovery.py`) does:

- **Install only.** `python scripts/boutique_chaos_recovery.py install` reads
  the node list, then runs two Helm installs and one annotation; it changes
  nothing else: Chaos Mesh chart 2.8.2
  with namespace filtering (`deploy/chaos-mesh/values.yaml`), the
  `chaos-mesh.org/inject=enabled` annotation on the connected namespace, and
  metrics-server chart 3.12.2 (`deploy/metrics-server/values.yaml`) with an
  image you must pin by digest. It creates no cluster. Any other context, and
  any name containing `sentinel-lab`, is refused before a command runs.
  Before either Helm install it reads the node list and refuses a context
  whose only node is not `sentinel-boutique-connected-control-plane`, so a
  context of the right name that points elsewhere gets nothing.
  `preflight` only reads: CRDs, ready chaos-mesh Pods, the namespace
  annotation, no chaos object left from an earlier run, and a Metrics API
  that returns at least one Pod's metrics for the namespace (it fails
  otherwise, because no threshold could then be complete).
- **Detection.** Every chaos run wraps the Observer with a read-only bridge
  (`runtime/metrics_bridge.py`) that turns metrics-server Pod usage into
  `k8s.pod.cpu.usage` (cores) and `k8s.pod.memory.working_set` (bytes)
  gauges for the Observer's existing thresholds. The threshold
  (`k8s.pod.cpu.usage` above 0.18 cores; shipping's CPU limit is 200m) is in
  the catalog, the same for all three scenarios. Quantities are converted
  exactly, Pods the target does not own are dropped, and a Pod without
  complete metrics gets no point, so the threshold rule raises no alert
  until every owned Pod is covered. The bridge is Observer evidence only,
  never a Guard input.
- **Planning.** Without `--agent` the code rule provider plans with a trusted
  read of the live template: Pods failing while the configuration equals the
  baseline give RESTART; a configuration change gives ROLLBACK. A metric-only
  incident (a metric alert while every Pod is ready) is escalated by a code
  rule in the Planner before any provider or model call
  (`runbooks/resource-saturation.md`). With `--agent` the model plans; its
  action list has no RESTART yet, so for N2 it can at best escalate.
- **Guard.** The run's policy allows RESTART for the target, at most once an
  hour. The Guard reads the restart history from the run's Executor ledger
  (`Experiment.guard_state`), so the budget is checked, not reported
  unknown. RESTART needs failing Pods (`RESTART_NOT_JUSTIFIED` otherwise) and
  an unchanged configuration (`RESTART_TEMPLATE_DRIFT` otherwise).
- **Attribution (N2).** A RESTART counts as Sentinel's repair only when the
  Verifier said RECOVERED, no current Pod is one the chaos selected (by the
  UID recorded at injection, or by name in the chaos object's
  `containerRecords`), the outcome window ended at least 60 s before the
  fault expires (420 s after the harness created it), and the same chaos
  object was still present with `desiredPhase: Run` after the outcome. The
  proof is `attribution.json`; a failed check fails the run.
- **The 2x2 negative matrix.** A forced wrong action goes to the restricted
  Executor with no approval (`approval.json` is null): nobody approves it,
  and the harness never writes an approval for it. The Executor's own Guard
  check must refuse it with the expected code and zero write attempts:
  ROLLBACK during the pod failure (`ROLLBACK_ALREADY_AT_BASELINE`, before
  planning), RESTART during a configuration drift (`RESTART_TEMPLATE_DRIFT`;
  after N2's recovery the harness sets R1's invalid PORT, refuses the
  RESTART, then restores the baseline as the administrator) and RESTART
  during CPU stress (`RESTART_NOT_JUSTIFIED`; in N3 this cell runs even when
  no incident opens, against a harness-made incident id, after NO_INCIDENT is
  scored). A REJECT with any other code (for example
  `RESTART_BUDGET_EXHAUSTED`) fails the run: it is not the cell's refusal.
  A Guard rejection outranks the missing approval. If the live state
  lets a forced action pass every check (for example a stressed Pod that also
  fails readiness), the Guard escalates for approval instead, nothing is
  written, and the run fails as "not refused". The correct diagonal is
  RESTART for the pod failure (N2) and ROLLBACK for the configuration fault
  (R1). Each refusal is in `<label>/proof.json`. The drift's rollout can
  move the Deployment's resourceVersion between the forced proposal's read
  and the Executor's. The Executor then answers CONFLICT before any Guard
  check, so nothing was refused. This happened in the first live N2 trial.
  Only that answer (CONFLICT with no Guard result and no write) is retried
  with a fresh proposal, at most three more times, 2 s apart. Each stale
  attempt is kept under `stale_resource_version_attempts` in the proof. A
  version that keeps moving still fails the cell.
- **True negative (T1, and N3 without an incident).** No incident passes
  only with a proof in `true-negative.json`: the fault confirmed injected,
  Observer samples spanning the detection window, at least one reading with
  complete metric thresholds, no Coordinator incident or action, no
  Executor ledger action and the template still at the baseline. For T1
  NO_INCIDENT scores correct. N3's label scores it acceptable:
  `scenario-result.json` keeps that score (`matched_expected` stays false),
  the run continues to the CPU cell only when the proof passes, and the PASS
  line names NO_INCIDENT and its score. Without the proof the run fails with
  an error that says NO_INCIDENT was scored and the proof failed.
- **Expected outcome.** A chaos run takes its expected outcome from the
  catalog label. Any other `--expect` is refused, with or without
  `--agent`, so `--expect recovered` cannot turn N3 or T1 into an approved
  repair. The one exception is `--agent --expect escalated` on N2, whose
  label accepts ESCALATE. `scripts/test-boutique-live.ps1` refuses an
  explicit `-Expect recovered` on a chaos scenario.
- **Cleanup.** The run deletes its own chaos object (by its run label) and
  waits for the finalizers; it never strips them. The run knows the object
  before its create call returns, so a create that fails after the API
  server stored the object is still cleaned up. The drift cell's
  administrator restore re-reads the Deployment and retries its conditional
  patch up to three times, since the drift's rollout can change the
  resourceVersion between the read and the patch.

Offline tests: `tests/test_boutique_chaos.py` (install, preflight,
injection with its UID record, attribution, true negative, emergency
cleanup, the harness's Guard policy and Observer wiring, and the N2, N3 and
T1 flows with the real Coordinator, Guard, approval session and Executor
against fakes),
`tests/test_cluster_metrics.py::MetricsBridgeTests`,
`tests/test_observer_telemetry.py::MetricsBridgeThresholdTests`,
`tests/test_guard_necessity.py::ChaosNegativeMatrixTests`,
`tests/test_planner.py::MetricOnlyTests` and
`tests/test_recovery_scenarios.py::ChaosCatalogTests`. Nothing here has run
against a cluster.

Assumptions the spike must confirm before any trial: that Chaos Mesh 2.8.2
names records `<namespace>/<pod>` (or `<namespace>/<pod>/<container>`) in
`status.experiment.containerRecords` and reports `desiredPhase` there (the
harness refuses an injection whose records it cannot parse), that it does
not re-select the new Pod after a restart, and whether CPU stress raises an
incident and of which kind.

### Live recipe (not yet run)

Run these on the host that holds the connected lab, in this order. Do not run
them while the `sentinel-dist` cluster (P4.01) is up, and never against
`sentinel-lab`. Have a team member sign off the catalog labels before the
first scored trial (step 4 onwards).

1. **Check the lab and pin metrics-server.** The connected lab must exist
   from an earlier connected run (its namespace is created by that run).

   ```powershell
   kubectl config get-contexts kind-sentinel-boutique-connected
   kubectl --context kind-sentinel-boutique-connected get namespace sentinel-boutique-connected
   docker stats --no-stream
   docker buildx imagetools inspect registry.k8s.io/metrics-server/metrics-server:v0.7.2
   ```

   Copy the `Digest:` line of the last command into the run notes; it is the
   pin for the next step. After the spikes, commit that digest (with any
   threshold change) in the pre-registration commit, so the pin can be
   reviewed in the repository before the scored trials.

   Pinned on 2026-11-13 (the multi-architecture index digest of
   metrics-server v0.7.2, resolved with the command above):
   `sha256:ffcb2bf004d6aa0a17d90e0247cf94f2865c8901dcab4427034c341951c239f9`.

   **Owner gate.** Step 2 installs a privileged chaos-daemon on the kind node
   and a metrics-server that skips kubelet TLS verification
   (`--kubelet-insecure-tls`; see B1 in the [threat model](threat-model.md)).
   Do not continue until the project owner has approved both for this lab.

2. **Install and preflight.**

   ```powershell
   python scripts/boutique_chaos_recovery.py install --metrics-server-digest sha256:<digest from step 1>
   python scripts/boutique_chaos_recovery.py preflight
   ```

   The preflight prints the chaos-mesh Pods and `pod_metrics_items`, and it
   fails when the Metrics API returns no Pod metrics. Stop if it fails.

3. **Spikes first (about 30 minutes each).** Re-selection and record format:

   ```powershell
   python -c "import json; from experiments.boutique.harness.chaos import scenario; print(json.dumps(scenario('pod', 'sentinel-boutique-connected', 'spike-pod', 420, 'shippingservice')))" | kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected create -f -
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get podchaos spike-pod -o jsonpath="{.status.experiment}"
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected delete pod -l app=shippingservice --wait=false
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get pods -l app=shippingservice -w
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get podchaos spike-pod -o jsonpath="{.status.experiment}"
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected delete podchaos spike-pod --wait=true
   ```

   Record the record ids, `desiredPhase`, whether the replacement Pod becomes
   Ready and whether its name joins `containerRecords`. If Chaos Mesh
   re-selects it, the measured answer for N2 is NOT_RECOVERED, then the
   restart budget, then ESCALATE; record that and do not claim recovery.
   Then CPU stress, watching usage and readiness for five minutes:

   ```powershell
   python -c "import json; from experiments.boutique.harness.chaos import scenario; print(json.dumps(scenario('cpu', 'sentinel-boutique-connected', 'spike-cpu', 300, 'shippingservice')))" | kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected create -f -
   kubectl --context kind-sentinel-boutique-connected get --raw /apis/metrics.k8s.io/v1beta1/namespaces/sentinel-boutique-connected/pods
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get pods -l app=shippingservice -w
   kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected delete stresschaos spike-cpu --wait=true
   ```

   Record the peak `cpu` of the shipping Pod and whether it stayed Ready. If
   0.18 cores is never exceeded, or is exceeded at rest, change
   `observer_thresholds` in a pre-registration commit before step 4.

   Spike results on 2026-11-13, in the connected lab on one host:
   - Pod failure: Chaos Mesh 2.8.2 named the record
     `sentinel-boutique-connected/<pod>` with `desiredPhase: Run`.
   - Pod failure: after the selected Pod was deleted, the replacement was
     not re-selected. It was Ready within about 15 s, and the chaos object
     still listed only the old Pod.
   - CPU stress: the shipping Pod used about 0.0005 cores at rest. Under
     stress it held at about 0.200 cores, its limit, for the whole 300 s.
   - CPU stress: the Pod stayed Ready with no restarts.
   - So the 0.18-core threshold is crossed only under stress, and it is
     unchanged.

4. **Pod failure x5 (N2).** Each run measures a fresh baseline, injects,
   refuses the forced ROLLBACK, plans RESTART, asks for approval, restarts,
   verifies, checks attribution, deletes the chaos object, then runs the
   drift cell:

   ```powershell
   foreach ($trial in 1..5) {
       powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Connected -Scenario N2 -Approval auto-lab
   }
   ```

   Expected per run: `scenario-result.json` with RESTART, source rule, score
   correct and one Executor write; a new Pod UID; `attribution.json`
   attributed, with RECOVERED at least 60 s before expiry;
   `forced-rollback/proof.json` and `forced-restart-drift/proof.json` with
   REJECT and zero patch attempts; `summary.json` with
   `chaos_cleanup_confirmed` true. Use `-Approval interactive` for one run to
   show a person approving.

   The timing is tight. RECOVERED must come at least 60 s before the fault
   expires, so about 360 s after the harness created the chaos object, and
   that time covers detection (up to about 130 s), the forced ROLLBACK
   refusal, planning, approval and the Verifier's window (up to 150 s of
   convergence grace). An operator who takes long to approve in the
   interactive run can make attribution fail although the RESTART was
   right. Report such a run as an attribution failure; do not re-score it.

5. **CPU stress (N3) and the network control (T1).**

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Connected -Scenario N3
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Connected -Scenario T1
   ```

   Expected: N3 ends NEEDS_REVIEW with ESCALATE decided by the metric-only
   rule, zero writes, and `forced-restart/proof.json` REJECT
   `RESTART_NOT_JUSTIFIED`. N3 may instead record NO_INCIDENT, which its
   label scores acceptable: the run then continues only when
   `true-negative.json` passes, the CPU cell runs against a harness-made
   incident id, and the PASS line says NO_INCIDENT scored acceptable. If
   the stressed Pod fails readiness, that proof shows ESCALATE
   `OPERATOR_APPROVAL_REQUIRED`, zero writes and a failed run: record it as
   the measured result. T1 records NO_INCIDENT with a passing
   `true-negative.json`, zero writes and the storefront latency in
   `primary-storefront-impact.json`. Neither asks for approval.

6. **Agent variant.** Run N2 once with the model and report what it chose:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Connected -Scenario N2 -Agent -AgentConfig path/to/model.json -Expect escalated
   ```

   The model cannot choose RESTART, so ESCALATE (acceptable) is the best it
   can score; a ROLLBACK must be overridden by the eligibility rule or refused
   by the Guard with zero writes. Report the choice as it is.

7. **Collect, then optionally remove.** Collect each run folder as evidence.
   To return the lab to its earlier state:

   ```powershell
   helm uninstall chaos-mesh --kube-context kind-sentinel-boutique-connected --namespace chaos-mesh
   helm uninstall metrics-server --kube-context kind-sentinel-boutique-connected --namespace kube-system
   kubectl --context kind-sentinel-boutique-connected annotate namespace sentinel-boutique-connected chaos-mesh.org/inject-
   ```
