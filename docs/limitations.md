# Known limitations and defects

Sentinel is a single-host prototype. This page lists what it does not do yet
and the defects found in evaluation, so nobody has to discover them in a demo.

The register is [limitations.json](limitations.json). Everything between the
markers below is generated from it by `python scripts/evidence/summarize.py`;
do not edit it by hand. Each entry has:

- **kind**: `defect` (the code does the wrong thing) or `limitation` (missing
  scope, stated honestly);
- **status**: `open`, `mitigated` or `fixed` on this tree. Fixed entries stay
  as history and name the pull request;
- **evidence**: a `file:line` with the text expected on that line, a file, or a
  claim id from [evidence/claims.json](../evidence/claims.json);
- **planned**: the roadmap item that addresses it, and the GitHub issues.

`python scripts/evidence/limitations.py --strict` runs in CI. It fails when a
cited file is gone, a line no longer holds the quoted text or a claim id does
not exist, so a citation cannot silently go stale. To fix a defect, change its
status to `fixed`, add `fixed_by`, point its evidence at the test that proves
the fix and regenerate.

<!-- BEGIN GENERATED:limitations -->
| Entry | Kind | Status | Title | Planned | Issues |
| --- | --- | --- | --- | --- | --- |
| [L-CLOCK-ORDERING](#l-clock-ordering) | defect | open | Lifecycle ordering compares wall-clock timestamps written by different components | P3.03 | #18, #63 |
| [L-AGENT-JOURNAL](#l-agent-journal) | defect | open | Agent mode bypasses the durable Planner journal | P2.01 | #14, #54 |
| [L-AGENT-APPROVAL](#l-agent-approval) | defect | open | Live runs use pre-authorised lab approval, not an authenticated operator | P2.02, P2.03 | #10 |
| [L-RAW-LOGS-PROMPT](#l-raw-logs-prompt) | defect | open | Raw container log text reaches the model | P2.04 | #51, #56 |
| [L-OBSERVER-ZERO-REPLICAS](#l-observer-zero-replicas) | defect | open | A Deployment scaled to zero replicas is treated as healthy | P5.07 | none |
| [L-EXEC-WEDGE](#l-exec-wedge) | defect | mitigated | A write rejected with its resource version unchanged still waits in NEEDS_RECONCILIATION | P1.11, P2.06, P5.08 | #61 |
| [L-EXEC-REDELIVERY](#l-exec-redelivery) | defect | mitigated | A replacement Executor without the ledger row reported CONFLICT for a write that was applied | P1.10, P3.01, P4.01 | #18, #58 |
| [L-NO-LOGGING](#l-no-logging) | defect | mitigated | Structured logs cover only the networked entrypoints, and no live run has collected them | P1.07, P3.02 | #62 |
| [L-SINGLE-COORDINATOR](#l-single-coordinator) | limitation | open | The Coordinator is one process with one SQLite file | P3.06, P4.02, P5.01 | #62, #63 |
| [L-HOST-LOCAL-EXECUTOR](#l-host-local-executor) | limitation | open | The Executor ledger and target locks are host-local | P3.04 | #18, #58, #59 |
| [L-NO-FENCING](#l-no-fencing) | limitation | open | No fencing of abandoned or revoked writes across hosts | P2.06, P3.04, P3.05 | #18, #58, #61 |
| [L-SINGLE-HOST-DISTRIBUTION](#l-single-host-distribution) | limitation | open | The networked worker layer has only run on one host | P3.01, P4.01 | #62, #63 |
| [L-ONE-FAULT-ONE-ACTION](#l-one-fault-one-action) | limitation | open | One fault type and one repair action | P2.08, P3.08, P3.09 | #74, #76 |
| [L-NO-DIAGNOSIS-ACCURACY](#l-no-diagnosis-accuracy) | limitation | open | No diagnosis-accuracy evaluation | P2.07, P4.04 | #17, #75 |
| [L-OVERLAPPING-FAULTS](#l-overlapping-faults) | limitation | open | Only one fault at a time has been verified | P4.05 | #77, #87 |
| [L-SECURITY-SHIPPING-ONLY](#l-security-shipping-only) | limitation | open | The security lab protects only the shipping path | P4.06, P5.04, P5.05 | #69, #71 |
| [L-UNGATED-CVES](#l-ungated-cves) | limitation | open | Workload image CVEs raise alerts but gate nothing | P5.02, P5.03 | #72, #73 |
| [L-CHAOS-NOT-WIRED](#l-chaos-not-wired) | limitation | open | Chaos Mesh faults are injection only, and HTTP and DNS faults do not run on this host | P4.03 | #76 |
| [L-EVIDENCE-INFERRED](#l-evidence-inferred) | limitation | open | All committed evidence has inferred provenance | P1.01, P2.09 | #63 |
| [L-NO-RELEASE](#l-no-release) | limitation | open | No tagged, scanned release image | P2.09, P2.10 | #63, #73 |
| [L-LAB-CERTS](#l-lab-certs) | limitation | open | Security lab certificates are two-day fixtures with no rotation | P5.06 | #70 |
| [L-MODEL-LATENCY](#l-model-latency) | limitation | mitigated | Model cold loads make agent planning slow and variable | P1.12 | #85, #87 |
| [L-UNPINNED-BASE-IMAGES](#l-unpinned-base-images) | limitation | mitigated | Sentinel component images used unpinned base image tags | P1.09 | #73 |
| [L-VERIFIER-FIRST-READING](#l-verifier-first-reading) | defect | fixed | The Verifier judged NOT_RECOVERED on its first reading after a patch | none | #56, #87 |
| [L-RECONCILE-AS-EXECUTE](#l-reconcile-as-execute) | defect | fixed | An executor.reconcile command was processed as a new execution | none | #61 |
| [L-GUARD-LOCK-KUBECTL](#l-guard-lock-kubectl) | defect | fixed | The Guard held a SQLite write lock across kubectl and passed target names unchecked | none | none |
| [L-AGENT-FAILURE-HIDDEN](#l-agent-failure-hidden) | defect | fixed | Agent planning failures were not shown on the dashboard or in summary.json | none | #87 |
| [L-CHAOS-SCENARIOS](#l-chaos-scenarios) | defect | fixed | The documented Chaos Mesh network, Pod and DNS scenarios could not run as written | none | #76 |
| [L-SAMPLER-READ-ABORT](#l-sampler-read-abort) | defect | fixed | One failed kubectl read during Verifier sampling aborted the pass | none | none |
| [L-PACKAGED-UTF8](#l-packaged-utf8) | defect | fixed | The packaged runner failed on a UTF-8 console | none | none |
| [L-KUBECTL-CONTEXT](#l-kubectl-context) | defect | fixed | Lab scripts left the user's kubectl context changed or unset | none | none |

### L-CLOCK-ORDERING

**Lifecycle ordering compares wall-clock timestamps written by different components.** Defect, open; recorded 2026-11-12. Planned: P3.03. Issues: #18, #63.

- Impact: The Coordinator refuses a result whose timestamp is not later than the evidence it follows. If the Executor's clock is behind the Guard's, an applied execution result is refused with HTTP 400, and the remote worker retries it forever because it gives up only on 404 or 409. Lease renewal also mixes the Coordinator's and the worker's clocks.
- Workaround: Run every component on one host, so they share one clock. All committed runs did.
- Evidence: `services/coordinator/core.py:149-150`; `services/coordinator/core.py:169-170`; `runtime/worker_http.py:157`; `runtime/remote_worker.py:132`; `runtime/remote_worker.py:73`.

### L-AGENT-JOURNAL

**Agent mode bypasses the durable Planner journal.** Defect, open; recorded 2026-11-11. Planned: P2.01. Issues: #14, #54.

- Impact: Agent planning calls the RCA engine directly instead of the PlannerStore journal. A failed agent run saves no ESCALATE, nothing retries it, and the incident stays OPEN.
- Workaround: Read the run's summary.json and dashboard event, then handle the incident by hand. The harness removes the fault as an administrator restore, which is not Sentinel recovery.
- Evidence: `experiments/boutique/harness/agent_live.py:239`; `docs/planner.md`; claim `C-AGENT-FAIL-CLOSED`.

### L-AGENT-APPROVAL

**Live runs use pre-authorised lab approval, not an authenticated operator.** Defect, open; recorded 2026-11-11. Planned: P2.02, P2.03. Issues: #10.

- Impact: Agent mode records a pre-authorised approval and the harness approves in code. The authenticated approval service exists but no live run uses it, so no evidence shows a person deciding.
- Workaround: Treat every live result as lab approval. Use the authenticated approval service only with explicit Guard/Executor wiring (docs/approvals.md).
- Evidence: `experiments/boutique/harness/agent_live.py:271`; `experiments/recovery/live.py:327`; claim `C-CONNECTED-HARNESS`.

### L-RAW-LOGS-PROMPT

**Raw container log text reaches the model.** Defect, open; recorded 2026-11-11. Planned: P2.04. Issues: #51, #56.

- Impact: Container log lines are exported and quoted into model context. A crafted log line could steer the model towards a rollback to the measured baseline. Guard and the Executor still enforce the target, baseline and resourceVersion checks.
- Workaround: None in code. The prompt tells the model that telemetry is untrusted, which is not a control.
- Evidence: `experiments/boutique/harness/agent_live.py:95`; `agent/tools.py:193-194`.

### L-OBSERVER-ZERO-REPLICAS

**A Deployment scaled to zero replicas is treated as healthy.** Defect, open; recorded 2026-11-12. Planned: P5.07. Issues: none.

- Impact: With no other failure signal, desired replicas of 0 produce no incident, so an accidental scale-down is not detected.
- Workaround: None. Only the invalid-PORT fault is in scope today.
- Evidence: `services/observer/core.py:409-410`.

### L-EXEC-WEDGE

**A write rejected with its resource version unchanged still waits in NEEDS_RECONCILIATION.** Defect, mitigated; recorded 2026-11-11. Planned: P1.11, P2.06, P5.08. Issues: #61.

- Impact: Fixed in code for every case where the write can no longer commit. Reconciliation is now a pure decision table: a deleted or recreated target, or a resourceVersion that moved without this action's evidence, ends SUPERSEDED, which the Coordinator records as FAILED so a new proposal can follow. Still open: kubectl error text is not classified (the DEFINITE_REJECTION and FENCED rows are reserved), so a rejection that leaves the same UID at the same resourceVersion, such as an admission denial, cannot be told apart from a request still in flight. That action stays NEEDS_RECONCILIATION and the workload stays excluded from repair; after 600 s the Executor adds one 'Operator action required' result. Live verification pending: the wedge harness has not been run against a cluster and no bundle is committed.
- Workaround: For the unchanged-version case, an operator inspects the Deployment and clears the Executor ledger row by hand. The deleted, recreated and moved-version cases need no workaround.
- Evidence: `services/executor/kubernetes_adapter.py:201`; `services/executor/reconciliation.py:18-19`; `services/executor/reconciliation.py:163`; `services/executor/core.py:37`; `tests/test_executor_reconciliation_table.py:255`; `tests/test_executor_reconciliation_table.py:287`; `scripts/validation/test_executor_wedge_live.py`.

### L-EXEC-REDELIVERY

**A replacement Executor without the ledger row reported CONFLICT for a write that was applied.** Defect, mitigated; recorded 2026-11-12. Planned: P1.10, P3.01, P4.01. Issues: #18, #58.

- Impact: Fixed in code, live verification pending. An Executor that finds the resourceVersion moved now looks for this action's evidence annotation on the Deployment and reports APPLIED for a peer's write without writing again. A host with no ledger row journals the action as in doubt and reconciles from the proposal and live state, so it can never start that write. Decision-table and integration tests cover both. One gap stays: if the peer's write was later overwritten, execute() reports CONFLICT where reconcile() reports SUPERSEDED; the Coordinator records both as FAILED. No run has used more than one Executor host.
- Workaround: Run one Executor with one ledger directory, as every documented run does, until a multi-host run verifies this.
- Evidence: `services/executor/core.py:343`; `services/executor/core.py:287`; `tests/test_executor_reconciliation_table.py:350`; `tests/test_executor_reconciliation_table.py:405`; `services/executor/store.py:3`.

### L-NO-LOGGING

**Structured logs cover only the networked entrypoints, and no live run has collected them.** Defect, mitigated; recorded 2026-11-11. Planned: P1.07, P3.02. Issues: #62.

- Impact: Fixed in code for the networked processes: shared/log.py writes allowlisted JSON lines; the Coordinator server, worker HTTP, approval HTTP and worker service entrypoints configure it; every request gets an access line and every 401 or 403 an auth.denied line; lifecycle transitions are journalled; and every broad except handler re-raises or logs the error class, which CI enforces. Not covered: the live harnesses, agent mode and the in-process pipelines that every recorded run used never configure logging, so their log events are dropped. Exception messages are never logged, only class names, by design. There is no cross-process tracing. No live run has collected logs yet.
- Workaround: For recorded runs, read the per-run JSON evidence files and the dashboard event journal. For networked runs, collect each process's stderr with SENTINEL_LOG_FORMAT=json.
- Evidence: `shared/log.py:235`; `runtime/worker_http.py:259`; `services/executor/core.py:221-222`; `docs/observability.md`; `tests/test_except_audit.py:39`.

### L-SINGLE-COORDINATOR

**The Coordinator is one process with one SQLite file.** Limitation, open; recorded 2026-11-11. Planned: P3.06, P4.02, P5.01. Issues: #62, #63.

- Impact: No replica takes over if the Coordinator host or its volume is lost; work waits until it returns. Workers back off for up to 60 s, which lengthens recovery after a restart.
- Workaround: Restart the Coordinator on the same volume; saved requests replay from SQLite.
- Evidence: `deploy/sentinel/coordinator.yaml:62-64`; `services/coordinator/store.py:133`; `runtime/worker_transport.py:103`.

### L-HOST-LOCAL-EXECUTOR

**The Executor ledger and target locks are host-local.** Limitation, open; recorded 2026-11-11. Planned: P3.04. Issues: #18, #58, #59.

- Impact: Two Executors on different hosts would not exclude each other. Safety would then rest on the resourceVersion-conditional patch alone.
- Workaround: Run every Executor on one host with one ledger and lock directory.
- Evidence: `services/executor/store.py:1-4`; `services/executor/store.py:91`.

### L-NO-FENCING

**No fencing of abandoned or revoked writes across hosts.** Limitation, open; recorded 2026-11-12. Planned: P2.06, P3.04, P3.05. Issues: #18, #58, #61.

- Impact: Revocation is check-then-act: a kubectl patch can land up to 15 s after the last authority check, and the resourceVersion test does not stop a stale write if nobody else wrote in between. RBAC limits verbs and resources, not Deployment fields.
- Workaround: Single-host runs only; the restricted adapter supplies the exact conditional JSON Patch.
- Evidence: `deploy/sentinel/executor-rbac.yaml:3`; `services/executor/kubernetes_adapter.py:28`.

### L-SINGLE-HOST-DISTRIBUTION

**The networked worker layer has only run on one host.** Limitation, open; recorded 2026-11-11. Planned: P3.01, P4.01. Issues: #62, #63.

- Impact: The HTTPS delivery layer is tested in-process over 127.0.0.1. It has never run as separate processes, containers or nodes under network faults. Containers or kind nodes on one Docker host would still not be separate machines.
- Workaround: None; read the distributed design as untested beyond one host.
- Evidence: `tests/test_worker_transport.py:163`; `deploy/sentinel/COORDINATOR.md`.

### L-ONE-FAULT-ONE-ACTION

**One fault type and one repair action.** Limitation, open; recorded 2026-11-11. Planned: P2.08, P3.08, P3.09. Issues: #74, #76.

- Impact: Every live recovery is an invalid shipping PORT where ROLLBACK is always right, so no result shows a choice between actions. RESTART and SCALE are in the contract, but Guard and the Executor refuse them.
- Workaround: Quote results only for this fault and action.
- Evidence: `services/guard/core.py:161-162`; `services/executor/core.py:336`; claim `C-CONNECTED-HARNESS`.

### L-NO-DIAGNOSIS-ACCURACY

**No diagnosis-accuracy evaluation.** Limitation, open; recorded 2026-11-11. Planned: P2.07, P4.04. Issues: #17, #75.

- Impact: There is no committed RCAEval result and no comparison of retrieval alone, retrieval with validation, and full Sentinel. Agent results show recovery on one fault, not diagnosis accuracy.
- Workaround: None; do not quote agent recovery counts as accuracy.
- Evidence: claim `C-AGENT-ALL`; `agent/datasets/README.md`.

### L-OVERLAPPING-FAULTS

**Only one fault at a time has been verified.** Limitation, open; recorded 2026-11-10. Planned: P4.05. Issues: #77, #87.

- Impact: Concurrent incidents queue per workload in code, but two simultaneous faults and dependency-safe recovery have never been run live.
- Workaround: Inject one fault per run.
- Evidence: `docs/planner.md`.

### L-SECURITY-SHIPPING-ONLY

**The security lab protects only the shipping path.** Limitation, open; recorded 2026-11-11. Planned: P4.06, P5.04, P5.05. Issues: #69, #71.

- Impact: The other Boutique services accept plaintext from any Pod and have no NetworkPolicy. The protected lab has never been combined with guarded recovery.
- Workaround: Quote security results for the shipping path only.
- Evidence: claim `C-SECURITY-LAB`; `docs/security.md`.

### L-UNGATED-CVES

**Workload image CVEs raise alerts but gate nothing.** Limitation, open; recorded 2026-11-11. Planned: P5.02, P5.03. Issues: #72, #73.

- Impact: The 11 November evaluation found High or Critical findings in 11 of 13 images. The scan monitor raises dashboard alerts; nothing in Guard, admission or CI blocks a vulnerable image. CI now fails on known vulnerabilities in the locked Python packages (pip-audit) and the dashboard's npm packages, but it scans no image.
- Workaround: Review scan alerts by hand before a demo.
- Evidence: `scripts/security/watch_security.py:1`; `scripts/security/watch_security.py:68`; `.github/workflows/checks.yml:46`.

### L-CHAOS-NOT-WIRED

**Chaos Mesh faults are injection only, and HTTP and DNS faults do not run on this host.** Limitation, open; recorded 2026-11-11. Planned: P4.03. Issues: #76.

- Impact: Sentinel takes no recovery action on Chaos Mesh faults. The HTTP fault needs a kernel module the Docker Desktop WSL2 kernel lacks, and the DNS fault needs a shell the hardened Boutique pods do not have.
- Workaround: Use the CPU, memory, Pod and network faults as injection demonstrations only.
- Evidence: claim `C-CHAOS-ALL`; `experiments/boutique/harness/chaos.py:18`; `docs/chaos.md`.

### L-EVIDENCE-INFERRED

**All committed evidence has inferred provenance.** Limitation, open; recorded 2026-11-12. Planned: P1.01, P2.09. Issues: #63.

- Impact: Every bundle predates provenance stamping, so its source revision is a best guess. Runs now stamp a provenance block, but no live run has produced a stamped bundle yet, so no bundle proves which commit produced it.
- Workaround: Quote each claim with its scope caveat, which says so.
- Evidence: claim `C-CONNECTED-HARNESS`; `evidence/README.md`.

### L-NO-RELEASE

**No tagged, scanned release image.** Limitation, open; recorded 2026-11-11. Planned: P2.09, P2.10. Issues: #63, #73.

- Impact: The packaged runner is built locally from the checkout. It was rebuilt and scanned for PR #98, but no image is tagged or published, and that scan is not a committed evidence bundle. A release preflight, a tag-triggered build with a Trivy gate, SBOMs and checksums now exist (docs/release.md) but have not run: no tag has been pushed, and the launcher still names the 0.3.0 runner.
- Workaround: Build the runner from the revision you are testing.
- Evidence: claim `C-PACKAGED`; `scripts/packaged-demo-common.ps1:5`; `.github/workflows/release.yml`.

### L-LAB-CERTS

**Security lab certificates are two-day fixtures with no rotation.** Limitation, open; recorded 2026-11-11. Planned: P5.06. Issues: #70.

- Impact: Certificates expire after two days and are not rotated, revoked or bound to a workload identity.
- Workaround: Recreate the lab for each session.
- Evidence: `scripts/security/security_lab.py:119`.

### L-MODEL-LATENCY

**Model cold loads make agent planning slow and variable.** Limitation, mitigated; recorded 2026-11-10. Planned: P1.12. Issues: #85, #87.

- Impact: Incident-to-repair time in agent mode had a long tail (docs/results.md). The two measured cold calls took up to 41 s with model loads up to 26 s, and one 356 s decision stage holds only 53 s of recorded model calls, so its cause is not established. Fixed in code, live verification pending: each model call now has an overall deadline, a response size cap, no redirects or proxies and a schema check; agent mode runs a preflight and a warm-up call before any fault is injected and records model-runtime.json; keep_alive is configurable. The per-call timeout still defaults to 300 s. No agent run has been repeated since, so the tail with warm-up is unmeasured.
- Workaround: Keep the model warm with keep_alive and check the warm-up load time in model-runtime.json after each run.
- Evidence: `agent/llm.py:259`; `agent/llm.py:416`; `experiments/boutique/harness/agent_live.py:169`; `tests/test_llm_transport.py`.

### L-UNPINNED-BASE-IMAGES

**Sentinel component images used unpinned base image tags.** Limitation, mitigated; recorded 2026-11-12. Planned: P1.09. Issues: #73.

- Impact: Fixed in code: every FROM line, the frontend stage included, is pinned by digest, a test fails on an unpinned base, and Dependabot proposes digest updates. It is recorded as mitigated, not fixed, until the pull request that merges it is known. Images built for earlier evidence used unpinned bases.
- Workaround: Not needed for new builds. Record the built image id with each run.
- Evidence: `deploy/docker/coordinator.Dockerfile:1`; `deploy/docker/guard.Dockerfile:2`; `deploy/docker/observer.Dockerfile:1`; `tests/test_supply_chain_pins.py:57`.

### L-VERIFIER-FIRST-READING

**The Verifier judged NOT_RECOVERED on its first reading after a patch.** Defect, fixed by #89; recorded 2026-11-11. Planned: none. Issues: #56, #87.

- Impact: A repair that worked was recorded as failed while gRPC clients were still reconnecting, so agent runs ended NOT_RECOVERED.
- Workaround: Not needed; an opt-in convergence grace sets aside only a leading run of unhealthy readings.
- Evidence: `services/verifier/core.py:53`; `tests/test_verifier.py:208`.

### L-RECONCILE-AS-EXECUTE

**An executor.reconcile command was processed as a new execution.** Defect, fixed by #93; recorded 2026-11-11. Planned: none. Issues: #61.

- Impact: A reconcile command could start a write instead of only inspecting a journaled one.
- Workaround: Not needed.
- Evidence: `runtime/executor_pipeline.py:184`; `tests/test_executor_integration.py:255`.

### L-GUARD-LOCK-KUBECTL

**The Guard held a SQLite write lock across kubectl and passed target names unchecked.** Defect, fixed by #93; recorded 2026-11-11. Planned: none. Issues: none.

- Impact: A slow cluster read blocked other workers for up to 30 s, and a target name could be parsed by kubectl as an option.
- Workaround: Not needed.
- Evidence: `tests/test_guard_integration.py:172`; `tests/test_guard_integration.py:193`.

### L-AGENT-FAILURE-HIDDEN

**Agent planning failures were not shown on the dashboard or in summary.json.** Defect, fixed by #92; recorded 2026-11-11. Planned: none. Issues: #87.

- Impact: A failed agent run looked like a stalled one, and Ollama's own error text was discarded. The incident still stays OPEN; see L-AGENT-JOURNAL.
- Workaround: Not needed.
- Evidence: `experiments/boutique/harness/agent_live.py:258`; `tests/test_live_agent.py:164`.

### L-CHAOS-SCENARIOS

**The documented Chaos Mesh network, Pod and DNS scenarios could not run as written.** Defect, fixed by #90; recorded 2026-11-11. Planned: none. Issues: #76.

- Impact: Chaos Mesh rejected the network scenario, the Pod fault run was always marked failed, and the DNS fault could not work on the hardened pods.
- Workaround: Not needed; DNS is now refused before any cluster work.
- Evidence: `tests/test_boutique_chaos.py:45`; `tests/test_boutique_chaos.py:51`; `tests/test_boutique_chaos.py:193`.

### L-SAMPLER-READ-ABORT

**One failed kubectl read during Verifier sampling aborted the pass.** Defect, fixed by #94; recorded 2026-11-11. Planned: none. Issues: none.

- Impact: A single transient read error ended verification with no outcome.
- Workaround: Not needed.
- Evidence: `tests/test_verifier_pipeline.py:476`.

### L-PACKAGED-UTF8

**The packaged runner failed on a UTF-8 console.** Defect, fixed by #94; recorded 2026-11-11. Planned: none. Issues: none.

- Impact: A byte-order mark before the image stream made the image import fail on code page 65001.
- Workaround: Not needed.
- Evidence: `tests/test_packaged_demo.py:58`.

### L-KUBECTL-CONTEXT

**Lab scripts left the user's kubectl context changed or unset.** Defect, fixed by #91; recorded 2026-11-11. Planned: none. Issues: none.

- Impact: Creating and deleting lab clusters in the default kubeconfig left the user's context pointing elsewhere or unset. Follow-up fixes for the other launchers are PRs #95 and #96.
- Workaround: Not needed.
- Evidence: `tests/test_lab_kubeconfig.py:40`; `tests/test_security_lab_context.py`.
<!-- END GENERATED:limitations -->
