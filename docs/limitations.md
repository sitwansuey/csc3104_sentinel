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
| [L-OBSERVER-ZERO-REPLICAS](#l-observer-zero-replicas) | defect | open | A Deployment scaled to zero replicas is treated as healthy | P5.07 | none |
| [L-ORPHANED-EXECUTOR-LEDGER](#l-orphaned-executor-ledger) | defect | open | A crashed Executor whose peer finished the action keeps it in doubt and refuses that workload | P3.04 | #18, #58 |
| [L-EXEC-WEDGE](#l-exec-wedge) | defect | mitigated | A write rejected with its resource version unchanged waits in NEEDS_RECONCILIATION unless fencing is on | P2.06, P5.08 | #61 |
| [L-EXEC-REDELIVERY](#l-exec-redelivery) | defect | mitigated | A replacement Executor without the ledger row reported CONFLICT for a write that was applied | P3.01, P4.01 | #18, #58 |
| [L-CLOCK-ORDERING](#l-clock-ordering) | defect | mitigated | Lifecycle ordering compared wall-clock timestamps written by different components | P3.03, P4.01 | #18, #63 |
| [L-AGENT-JOURNAL](#l-agent-journal) | defect | mitigated | Agent mode bypasses the durable Planner journal | P2.01 | #14, #54 |
| [L-AGENT-APPROVAL](#l-agent-approval) | defect | mitigated | No live run yet shows a person approving through the authenticated API | P2.02, P2.03 | #10 |
| [L-RAW-LOGS-PROMPT](#l-raw-logs-prompt) | defect | mitigated | Sentinel's planning prompts carry log text only cleaned and quoted, and the injection gate is a pattern list no live run has exercised | P2.04, P2.05, P4.04 | #51, #56 |
| [L-FREE-TEXT-DECISION](#l-free-text-decision) | defect | mitigated | The live diagnosis was model free text and the rollback choice had no change evidence | P2.05, P4.04 | #15, #52, #53, #74, #87 |
| [L-NO-LOGGING](#l-no-logging) | defect | mitigated | Structured logs cover only the networked entrypoints, and no live run has collected them | P1.07, P3.02 | #62 |
| [L-SINGLE-COORDINATOR](#l-single-coordinator) | limitation | open | The Coordinator is one process with one SQLite file | P4.02, P5.01 | #62, #63 |
| [L-HOST-LOCAL-EXECUTOR](#l-host-local-executor) | limitation | open | The Executor ledger and target locks are host-local | P3.04 | #18, #58, #59 |
| [L-NO-FENCING](#l-no-fencing) | limitation | open | No fencing of abandoned or revoked writes across hosts | P2.06, P3.04, P3.05 | #18, #58, #61 |
| [L-SINGLE-HOST-DISTRIBUTION](#l-single-host-distribution) | limitation | open | The networked worker layer has only run on one host | P3.01, P4.01 | #62, #63 |
| [L-RESTART-BUDGET-LOCAL](#l-restart-budget-local) | limitation | open | The restart budget is kept per Executor ledger, and only local wiring gives it to the Guard | P4.03 | #74, #77 |
| [L-NO-DIAGNOSIS-ACCURACY](#l-no-diagnosis-accuracy) | limitation | open | No diagnosis-accuracy evaluation | P2.07, P4.04 | #17, #75 |
| [L-OVERLAPPING-FAULTS](#l-overlapping-faults) | limitation | open | Only one fault at a time has been verified | P4.05 | #77, #87 |
| [L-SECURITY-SHIPPING-ONLY](#l-security-shipping-only) | limitation | open | The security lab protects only the shipping path | P4.06, P5.04, P5.05 | #69, #71 |
| [L-UNGATED-CVES](#l-ungated-cves) | limitation | open | Workload image CVEs raise alerts but gate nothing | P5.02, P5.03 | #72, #73 |
| [L-CHAOS-NOT-WIRED](#l-chaos-not-wired) | limitation | open | Chaos Mesh faults are injection only, and HTTP and DNS faults do not run on this host | P4.03 | #76 |
| [L-NO-RELEASE](#l-no-release) | limitation | open | No tagged, scanned release image | P2.09, P2.10 | #63, #73 |
| [L-LAB-CERTS](#l-lab-certs) | limitation | open | Security lab certificates are two-day fixtures with no rotation | P5.06 | #70 |
| [L-RESTORE-DETECTION](#l-restore-detection) | limitation | mitigated | A Coordinator restored from a backup is detected, but lost acknowledged results are re-driven by hand | P4.02 | #62, #63 |
| [L-ONE-FAULT-ONE-ACTION](#l-one-fault-one-action) | limitation | mitigated | One fault type and one repair action | P2.08, P3.08, P3.09 | #74, #76 |
| [L-MODEL-LATENCY](#l-model-latency) | limitation | mitigated | Model cold loads make agent planning slow and variable | P2.01 | #85, #87 |
| [L-UNPINNED-BASE-IMAGES](#l-unpinned-base-images) | limitation | mitigated | Sentinel component images used unpinned base image tags | P1.09 | #73 |
| [L-EVIDENCE-INFERRED](#l-evidence-inferred) | limitation | mitigated | Most committed evidence has inferred provenance | P2.09 | #63 |
| [L-VERIFIER-FIRST-READING](#l-verifier-first-reading) | defect | fixed | The Verifier judged NOT_RECOVERED on its first reading after a patch | none | #56, #87 |
| [L-RECONCILE-AS-EXECUTE](#l-reconcile-as-execute) | defect | fixed | An executor.reconcile command was processed as a new execution | none | #61 |
| [L-GUARD-LOCK-KUBECTL](#l-guard-lock-kubectl) | defect | fixed | The Guard held a SQLite write lock across kubectl and passed target names unchecked | none | none |
| [L-AGENT-FAILURE-HIDDEN](#l-agent-failure-hidden) | defect | fixed | Agent planning failures were not shown on the dashboard or in summary.json | none | #87 |
| [L-CHAOS-SCENARIOS](#l-chaos-scenarios) | defect | fixed | The documented Chaos Mesh network, Pod and DNS scenarios could not run as written | none | #76 |
| [L-SAMPLER-READ-ABORT](#l-sampler-read-abort) | defect | fixed | One failed kubectl read during Verifier sampling aborted the pass | none | none |
| [L-PACKAGED-UTF8](#l-packaged-utf8) | defect | fixed | The packaged runner failed on a UTF-8 console | none | none |
| [L-KUBECTL-CONTEXT](#l-kubectl-context) | defect | fixed | Lab scripts left the user's kubectl context changed or unset | none | none |

### L-OBSERVER-ZERO-REPLICAS

**A Deployment scaled to zero replicas is treated as healthy.** Defect, open; recorded 2026-11-12. Planned: P5.07. Issues: none.

- Impact: With no other failure signal, desired replicas of 0 produce no incident, so an accidental scale-down is not detected.
- Workaround: None. Only the invalid-PORT fault is in scope today.
- Evidence: `services/observer/core.py:409-410`.

### L-ORPHANED-EXECUTOR-LEDGER

**A crashed Executor whose peer finished the action keeps it in doubt and refuses that workload.** Defect, open; recorded 2026-11-12. Planned: P3.04. Issues: #18, #58.

- Impact: Found by the P3.01 multi-process harness (S1b and S1c, separate processes on one host). An Executor killed after journalling its write intent, or after its patch, leaves the action WRITE_STARTED in its own ledger. When the other Executor takes the command and finishes the action, no command for it ever reaches the crashed host again, and the remote worker runs no restart scan of its ledger, so after a restart the row stays in doubt. From the code, not from a scenario, that host would then refuse every later action on the same workload with TargetBusy until someone reconciles the row (services/executor/store.py prepare(), cited below); no scenario runs a second action to show it. No extra write happens: each run kept one template write. When the crashed host is the only one to come back (S1e), it reconciles and fences its own write as designed.
- Workaround: After an Executor crash, reconcile its in-doubt rows with runtime/live_executor.py --reconcile on that host, or run one Executor per workload.
- Evidence: `services/executor/store.py:176`; `tests/test_distributed_processes.py:38`.

### L-EXEC-WEDGE

**A write rejected with its resource version unchanged waits in NEEDS_RECONCILIATION unless fencing is on.** Defect, mitigated; recorded 2026-11-11. Planned: P2.06, P5.08. Issues: #61.

- Impact: Fixed in code; verified live, one run each, for the moved-resourceVersion and replaced-target cases (a plain delete was not run live), with live verification pending for the fence. Reconciliation is a pure decision table: a deleted or recreated target, or a resourceVersion that moved without this action's evidence, ends SUPERSEDED, which the Coordinator records as FAILED so a new proposal can follow. The live wedge harness (C-EXECUTOR-WEDGE: one run per scenario on one Windows host with kind, 2026-11-11) showed main's Executor (d5b2ed9) staying NEEDS_RECONCILIATION over 5 reconcile passes with the workload still excluded, while 4afc879 resolved the moved-resourceVersion case (rv-race) as SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE and the replaced-target case (delete-recreate) as SUPERSEDED/TARGET_REPLACED, released the workload and applied a second proposal. kubectl error text is still not classified (the DEFINITE_REJECTION rows are reserved), so a rejection that leaves the same UID at the same resourceVersion, such as an admission denial, still looks like a request in flight. When fencing is configured (fence_after_seconds; off by default), a reconcile pass after the settle delay sends a metadata-only patch with the same UID and resourceVersion tests and this action's fence marker, journaled before the call. If it commits, the write never committed and never can, so the result is CONFLICT with basis FENCED and the workload is released; tests and the model checker cover it, and the model checker ends no run in doubt with fencing on. With fencing off, that action stays NEEDS_RECONCILIATION and after 600 s gets one 'Operator action required' result. Live verification pending for the fence: the admission-deny scenario that exercises it has not been run against a cluster and no bundle for it is committed; the 2026-11-11 runs predate the fence.
- Workaround: Turn fencing on: fence_after_seconds in the Executor worker configuration, or --fence-after with runtime/live_executor.py --reconcile. With fencing off, an operator inspects the Deployment and clears the Executor ledger row by hand for the unchanged-version case. The deleted, recreated and moved-version cases need no workaround.
- Evidence: `services/executor/kubernetes_adapter.py:243`; `services/executor/reconciliation.py:230`; `services/executor/core.py:47`; `services/executor/core.py:276-277`; `services/executor/kubernetes_adapter.py:214`; `tests/test_executor_reconciliation_table.py:325`; `tests/test_executor_reconciliation_table.py:357`; `tests/test_executor.py:651`; `tests/test_executor_reconcile_model.py:378`; `scripts/validation/test_executor_wedge_live.py`; claim `C-EXECUTOR-WEDGE`.

### L-EXEC-REDELIVERY

**A replacement Executor without the ledger row reported CONFLICT for a write that was applied.** Defect, mitigated; recorded 2026-11-12. Planned: P3.01, P4.01. Issues: #18, #58.

- Impact: Fixed in code; live verification is left to P3.01 and P4.01. An Executor that finds the resourceVersion moved now looks for this action's evidence annotation on the Deployment and reports APPLIED for a peer's write without writing again. A host given a reconcile command with no ledger row journals the action as in doubt and reconciles from the proposal and live state, so it never starts that write. An execute command on a fresh host can still write, and the resourceVersion test in the patch lets at most one write for the action commit. Decision-table and integration tests cover both. One gap stays: if the peer's write was later overwritten, execute() reports CONFLICT where reconcile() reports SUPERSEDED; the Coordinator records both as FAILED. The P3.01 multi-process harness runs two Executor processes with separate ledgers and locks on one host: in S1c a peer adopted a write committed by a crashed Executor and in S2 a stalled Executor adopted its peer's write, each with one template write. That is still one machine and a file-backed test cluster; no live run has used two Executors.
- Workaround: Run one Executor with one ledger directory, as every documented run does, until a multi-host run verifies this.
- Evidence: `services/executor/core.py:420`; `services/executor/core.py:364`; `tests/test_executor_reconciliation_table.py:420`; `tests/test_executor_reconciliation_table.py:475`; `services/executor/store.py:3`; `tests/test_distributed_processes.py:43`.

### L-CLOCK-ORDERING

**Lifecycle ordering compared wall-clock timestamps written by different components.** Defect, mitigated; recorded 2026-11-12. Planned: P3.03, P4.01. Issues: #18, #63.

- Impact: Fixed in code (P3.03); not run across machines or in kind. Worker results are now ordered by the Coordinator request they answer, not by the producers' clocks, so the wedge is gone: an answer to a superseded request gets HTTP 409 and is released, never resent for ever. Scenario S7 (both Executors on a gated test clock 300 s behind, separate processes on one Windows host, file mode, operator approval required) showed the wedge before the change (result refused with HTTP 400, incident READY). After it the result is accepted, but the Executor's own Guard re-check, which reads the same shifted clock, sees the Coordinator-stamped approval 300 s in its future and refuses: the action ends FAILED with no write and every invariant holding in 3 of 3 runs, with each Executor measuring an offset of 300.005 to 300.012 s. An Executor host whose whole clock is off by more than the bound therefore cannot complete an approved action; an in-bound peer can (S7b, 3 of 3 runs). Lease renewal now uses the lease length the Coordinator returns. Clocks still matter for freshness and expiry: the Guard tolerates a configured bound (max_clock_skew_seconds, off by default) and counts it against age and expiry; the Executor's local re-check and the Verifier's start check still compare their clock with timestamps from other processes, so skew beyond the bound ends an action FAILED or, by reading the code (no test), delays verification instead of wedging it. Workers measure their offset from the Coordinator (Cristian's algorithm) and, with a bound set, stop claiming and turn unready outside it. After a clock step this holds from the first exchange after the step: a request already claimed runs with the stepped clock until its next exchange, and a step smaller than the measurement uncertainty can go unseen. The offset is self-reported, so this guards against honest misconfiguration, not a dishonest worker. Messages accepted before store migration 4 have no high-water mark, so for an action in flight at the upgrade only the newest-request rule orders its results. Skew has only been simulated on one host, never measured between machines or in kind.
- Workaround: Keep host clocks synchronised (NTP), and set max_clock_skew_seconds for the workers and the Guard policy and --max-clock-skew-seconds for the Coordinator, so a host past the bound stops claiming work.
- Evidence: `services/coordinator/core.py:118`; `services/coordinator/core.py:193`; `runtime/worker_transport.py:255`; `runtime/remote_worker.py:157`; `services/guard/core.py:159`; `runtime/worker_http.py:319`; `runtime/live_executor.py:107`; `services/verifier/store.py:78`; `tests/test_distributed_processes.py:62`; `tests/test_clock_skew.py:675`.

### L-AGENT-JOURNAL

**Agent mode bypasses the durable Planner journal.** Defect, mitigated; recorded 2026-11-11. Planned: P2.01. Issues: #14, #54.

- Impact: Fixed in code: agent planning now runs through prepare() and the PlannerStore journal. A model failure is retried up to three times; an abstention, a diagnosis of another component, a NO_ACTION answer or a target changed during inference ends at once. Every failure ends in a saved, delivered ESCALATE, so the incident moves to NEEDS_REVIEW, and a saved proposal replays without another model call. Still open: the planning budget applies per attempt, so one incident can use three budgets, and the remote Planner worker has no agent mode. Live verification pending: no agent run has used the journal yet, and every committed agent bundle predates it, so those failed runs left the incident OPEN.
- Workaround: For runs recorded before this change, read the run's summary.json and dashboard event, then handle the incident by hand. The harness removes the fault as an administrator restore, which is not Sentinel recovery.
- Evidence: `experiments/boutique/harness/agent_live.py:257`; `services/planner/worker.py:167`; `services/planner/worker.py:181`; `tests/test_agent_journal.py:202`; `tests/test_agent_journal.py:279`; `docs/planner.md`; claim `C-AGENT-FAIL-CLOSED`.

### L-AGENT-APPROVAL

**No live run yet shows a person approving through the authenticated API.** Defect, mitigated; recorded 2026-11-11. Planned: P2.02, P2.03. Issues: #10.

- Impact: Fixed in code for the four live harness paths: the Executor experiment, Boutique walkthrough, provider mode and agent mode now create approvals through the authenticated approval API, served per run by a child process with generated tokens, and the Executor's final pre-write lookup reads them through ApprovalClient. Revocation, expiry or a stopped authority therefore fails closed with zero writes, and agent mode no longer records a pre-authorised approval. An interactive run needs a person to type the exact action ID; an unattended run must opt in to auto-lab approval, recorded under the automated-lab identity and never as a person. Agent and provider modes plan through the durable Planner journal and approve through the same session, and an agent run with --expect escalated asks for no approval and starts no authority. A Boutique run with the dashboard's lab controls can instead use ui approval: the dashboard shows the pending action, the operator types its action ID and clicks Approve or Revoke, and the dashboard server relays the decision with the run's operator credential (dashboard-operator by default); the browser holds only the lab session token, which any local process can also read, so it does not identify a person. This is proven offline with the real child process and a fake Kubernetes API, for agent mode and, since P2.04, for provider mode (run_provider_case with a real provider subprocess). Live verification pending: no live run has used this path, so no evidence yet shows a person approving, and every committed live bundle used pre-authorised lab approval. The file-backed negative cases still give the Executor a fixed approval record that never reaches the Coordinator; they prove refusals only. The Executor experiment's rival-process check reads a file copy of the managed record, not through ApprovalClient; the target lock refuses it before it reads the approval. The Verifier acceptance script (scripts/validation/test_verifier_connected_live.py) still calls the local approve() and reads the record straight from the store; it is outside this fix. Identities are bearer tokens over loopback HTTP, not a per-person login.
- Workaround: Treat committed live results as lab approval. For new evidence of a person deciding, run with -Approval interactive (the default) and check operator_approval in summary.json.
- Evidence: `experiments/recovery/live.py:376`; `experiments/recovery/live.py:390`; `runtime/approval_session.py:96`; `runtime/approval_session.py:315`; `experiments/recovery/live.py:365`; `experiments/recovery/live.py:445`; `scripts/validation/verifier_connected_lab.py:198`; `tests/test_approval_session.py:346`; `tests/test_approval_session.py:383`; `tests/test_approval_session.py:527`; `experiments/boutique/harness/agent_live.py:403`; `scripts/boutique_provider.py:30`; `tests/test_live_agent.py:496`; `tests/test_dashboard_approval_relay.py:246`; `tests/test_provider_approval.py:67`; claim `C-CONNECTED-HARNESS`.

### L-RAW-LOGS-PROMPT

**Sentinel's planning prompts carry log text only cleaned and quoted, and the injection gate is a pattern list no live run has exercised.** Defect, mitigated; recorded 2026-11-11. Planned: P2.04, P2.05, P4.04. Issues: #51, #56.

- Impact: Fixed in code; live verification pending. In agent mode, container log lines, Kubernetes event messages and incident signal details are now cleaned (control and format characters removed, NFKC applied, length capped), templated and quoted under untrusted_telemetry_evidence before a model reads them, and logs.csv keeps a cleaned copy with the hash of the raw line. A line that matches the curated instruction-like rules (override phrases, role markers, chat-template tokens, forged proposal fields, approval claims, Sentinel's own prompt headings) is withheld from every prompt: the model sees only its rule ids and hash. A code-owned gate in AgentPlanner then records an injection finding (rule ids and counts, decided_by rule) and ends the attempt in the fixed SUSPECTED_INJECTION escalation without calling the Recovery Planner, so flagged telemetry leads to ESCALATE and NEEDS_REVIEW with zero writes. The Recovery Planner no longer receives the Reporter's explanation or limitations, only cleaned prediction fields, evidence ids and quoted signals. The remote Planner worker (runtime/remote_adapters.py, OllamaProvider) runs the same rules over the raw incident signals before any model call: a flagged signal ends planning in the SUSPECTED_INJECTION escalation without a model call and the attempt's saved usage keeps the finding; otherwise its model reads the signals only quoted under untrusted_telemetry_evidence. This is proven offline with a 19-line adversarial corpus, a 26-line benign set that matches no rule, a recording scripted model and an obedient one, and for the remote worker with a recording fake Ollama server that always answers ROLLBACK and receives no request for any attack. Remaining risk: the rules are a pattern list, not a complete defence. A paraphrase they miss still reaches the model as a cleaned excerpt and can steer the diagnosis or the Recovery Planner within the Guard's limits. In agent mode the Reporter's component and reason now come from a code-owned vocabulary (L-FREE-TEXT-DECISION), so a steered diagnosis can only pick a wrong name from that list; inside the RCA agent the Analyzer's summary still reaches the Reporter. A false positive escalates a genuine incident, which is a safe failure; ordinary lines such as 'user: alice not found' or 'helm upgrade failed; action=rollback triggered' are flagged today. If an attempt on flagged telemetry fails in the model three times before its check runs, the incident ends in ATTEMPTS_EXHAUSTED and no finding is recorded. Still open: provider mode passes the raw planning context to the operator's own provider script, and the offline RCAEval CaseToolkit benchmark (agent/datasets/casetoolkit.py, not on the live path) sends upstream tool output, which can include raw log rows, to the model with no gate. Live verification pending: no live run has used this path, and neither the planned adversarial-fault run nor the three normal runs after it have been made.
- Workaround: For a paraphrase the rules miss, the remaining controls are rollback-only actions to a measured baseline, the Guard and authenticated approval. A SUSPECTED_INJECTION escalation is a prompt for review: find the flagged lines by message_sha256 in the attempt's logs.csv and the saved observation.
- Evidence: `agent/untrusted.py`; `agent/tools.py:205`; `experiments/boutique/harness/agent_live.py:89`; `services/planner/agent_provider.py:357`; `runtime/remote_adapters.py:192`; `runtime/remote_adapters.py`; `tests/test_untrusted_telemetry.py:524`; `tests/test_untrusted_telemetry.py:559`; `tests/test_untrusted_telemetry.py:573`; `tests/test_untrusted_telemetry.py:264`; `tests/test_untrusted_telemetry.py:624`; `tests/test_untrusted_telemetry.py:645`.

### L-FREE-TEXT-DECISION

**The live diagnosis was model free text and the rollback choice had no change evidence.** Defect, mitigated; recorded 2026-11-12. Planned: P2.05, P4.04. Issues: #15, #52, #53, #74, #87.

- Impact: Fixed in code; live verification pending. Before this change the live diagnosis's component and reason were model free text, and the Recovery Planner could not see whether the live template differed from the baseline, so a rollback choice could not be checked against evidence. In agent mode the Reporter now picks the component from the reviewed topology plus the target and the reason from a fixed six-reason taxonomy, and must cite the ids of collected tool results that name the component. An answer outside the vocabulary gets one correction and then abstains, and a citation that does not name the component abstains. Code computes change evidence: a bounded, redacted diff of the live Pod template against the healthy baseline, image digests, rollout facts and a bound on the change's age. A code rule decides whether ROLLBACK is eligible: the reason is one a rollback can repair, the component is the target, a signal shows a rollout, startup, crash, back-off or image pull symptom, the template differs from the baseline and the change is recent. Under the default gated policy an ineligible case ends in a fixed escalation without a Recovery Planner call, a model ROLLBACK for an ineligible case is overridden, and the decision is recorded with decided_by rule or model. The rule-only and model-only policies (P1 and P3) can be chosen in the agent config for evaluation. This is proven offline with a scripted model only. Remaining risk: the enum can push a confused model into a wrong but valid name; the citation check may raise the live abstention rate; the change-age bound comes from the baseline time, so an old baseline refuses a genuine recent change; the remote Planner worker and provider mode do not use this context. Live verification pending: no live run has used it. The planned checks are three standard PORT runs that must still reach RECOVERED with component shippingservice, and a fault without a template change that must end in ESCALATE (catalog scenario N1, see L-ONE-FAULT-ONE-ACTION).
- Workaround: To let the model decide without change evidence or enforcement of the eligibility rule, set decision_policy to model in -AgentConfig; the vocabulary and citation checks still apply. A refused rollback is an escalation for review: the attempt's journal context holds the eligibility facts and the change evidence.
- Evidence: `services/planner/taxonomy.py:94`; `services/planner/change.py`; `agent/agent.py`; `services/rca/core.py`; `services/planner/agent_provider.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`; `tests/test_planner_change_evidence.py`; `tests/test_planner_change_evidence.py`; `tests/test_rca_vocabulary.py`; `tests/test_rca_vocabulary.py`.

### L-NO-LOGGING

**Structured logs cover only the networked entrypoints, and no live run has collected them.** Defect, mitigated; recorded 2026-11-11. Planned: P1.07, P3.02. Issues: #62.

- Impact: Fixed in code for the networked processes: shared/log.py writes allowlisted JSON lines; the Coordinator server, worker HTTP, approval HTTP and worker service entrypoints configure it; every POST request gets one http.access line, or an auth.denied line for a 401 or 403, while GET /healthz, GET /readyz and GET requests for unknown paths (404) get none; lifecycle transitions are journalled; and every broad except handler re-raises or logs the error class, which CI enforces. Not covered: the live harnesses (the Executor wedge harness included), agent mode and the in-process pipelines that every recorded run used never configure logging, so their log events are dropped. Exception messages are never logged, only class names, by design. Since P3.02, W3C trace context follows an incident through outbox rows, claimed deliveries and worker HTTP requests, and spans are written as span.start and span.end log lines; approvals and the in-process pipelines add no spans of their own, there is no OpenTelemetry export, and no live run has produced a trace. No live run has collected logs yet.
- Workaround: For recorded runs, read the per-run JSON evidence files and the dashboard event journal. For networked runs, collect each process's stderr with SENTINEL_LOG_FORMAT=json and print the traces with scripts/trace_view.py.
- Evidence: `shared/log.py:237`; `runtime/worker_http.py:517`; `shared/tracing.py:123`; `services/executor/core.py:240-241`; `docs/observability.md`; `tests/test_except_audit.py:39`.

### L-SINGLE-COORDINATOR

**The Coordinator is one process with one SQLite file.** Limitation, open; recorded 2026-11-11. Planned: P4.02, P5.01. Issues: #62, #63.

- Impact: No replica takes over if the Coordinator host or its volume is lost; work waits until it returns. Since P3.06 a worker waits at most 15 s, with full jitter, between failed passes, instead of up to 60 s on a fixed schedule. Measured in file mode (separate processes on one Windows host, loopback, a shared and busy machine; not a cluster): after a 75 s outage the slowest of five workers claimed again 8.1 s (median of 6 restarts) after the restarted Coordinator was ready, against 56.7 s before; after a 30 s outage it was 10.4 s against 5.6 s, because the old fixed schedule happened to line up. Every claim still replays the whole pending outbox in one transaction, so claims per second fall as the backlog grows (about 40 to 55 with 50 to 200 commands queued, 21 to 23 with 1,000).
- Workaround: Restart the Coordinator on the same volume; saved requests replay from SQLite.
- Evidence: `deploy/sentinel/coordinator.yaml:62-64`; `services/coordinator/store.py:189`; `runtime/worker_service.py:26`; `tests/test_worker_backoff.py:60`; `experiments/distributed/measurements/p3-06/README.md`.

### L-HOST-LOCAL-EXECUTOR

**The Executor ledger and target locks are host-local.** Limitation, open; recorded 2026-11-11. Planned: P3.04. Issues: #18, #58, #59.

- Impact: Two Executors on different hosts would not exclude each other. Safety would then rest on the resourceVersion-conditional patch alone.
- Workaround: Run every Executor on one host with one ledger and lock directory.
- Evidence: `services/executor/store.py:1-4`; `services/executor/store.py:92`.

### L-NO-FENCING

**No fencing of abandoned or revoked writes across hosts.** Limitation, open; recorded 2026-11-12. Planned: P2.06, P3.04, P3.05. Issues: #18, #58, #61.

- Impact: Revocation is check-then-act: a kubectl patch can land up to 15 s after the last authority check, and the resourceVersion test does not stop a stale write if nobody else wrote in between. An Executor can now fence its own write that is still in doubt (P2.06, when configured), but nothing sends a fence when a lease expires or an approval is revoked, so such a write can still land. RBAC limits verbs and resources, not Deployment fields.
- Workaround: Single-host runs only; the restricted adapter supplies the exact conditional JSON Patch.
- Evidence: `deploy/sentinel/executor-rbac.yaml:3`; `services/executor/kubernetes_adapter.py:31`.

### L-SINGLE-HOST-DISTRIBUTION

**The networked worker layer has only run on one host.** Limitation, open; recorded 2026-11-11. Planned: P3.01, P4.01. Issues: #62, #63.

- Impact: Since P3.01 the Coordinator and every worker run as separate processes on one host, with their own state, credentials and TCP connections, under injected network, process and timing faults (CI, file-backed test cluster). That is still one machine: one kernel clock, one disk and loopback networking. The delivery layer has not run in containers, on separate nodes or against the kind lab (the harness's kind mode has not been run), and containers or kind nodes on one Docker host would still not be separate machines.
- Workaround: None; read the distributed design as untested beyond one host.
- Evidence: `tests/test_worker_transport.py:163`; `experiments/distributed/topology.py`; `deploy/sentinel/COORDINATOR.md`.

### L-RESTART-BUDGET-LOCAL

**The restart budget is kept per Executor ledger, and only local wiring gives it to the Guard.** Limitation, open; recorded 2026-11-12. Planned: P4.03. Issues: #74, #77.

- Impact: The Guard counts a target's recent restarts from live_state.restart_history, which the Executor reads from its own SQLite ledger under the target lock, so the budget survives an Executor restart. It supplies only the newest 1000 restarts, which is enough to decide any allowed budget (at most 100 per window). Executors on other hosts keep separate ledgers, so each host would have its own budget. The HTTP Guard worker and the live harnesses do not supply the history yet, so a RESTART there escalates as RESTART_BUDGET_UNKNOWN; only the offline demonstration wires it.
- Workaround: Run one Executor per target, as every committed run does. Treat RESTART as tested offline only.
- Evidence: `services/executor/store.py:306`; `services/guard/restart.py:121`; `experiments/validation/demo_offline_flow.py:102`.

### L-NO-DIAGNOSIS-ACCURACY

**No diagnosis-accuracy evaluation.** Limitation, open; recorded 2026-11-11. Planned: P2.07, P4.04. Issues: #17, #75.

- Impact: There is no committed RCAEval result and no comparison of retrieval alone, retrieval with validation, and full Sentinel. Agent results show recovery on one fault, not diagnosis accuracy. A pre-registered Online Boutique evaluation is prepared but not run: the protocol, the stratified sampler, the BARO and anomaly-rank baselines, a runner that locks the eval split until the dev runs are frozen, and a report with Wilson intervals and paired tests. All of it is tested on synthetic data only. No RCAEval telemetry has been downloaded, no model has been run under the protocol, and the licence and citation still need a person's confirmation.
- Workaround: None; do not quote agent recovery counts as accuracy.
- Evidence: claim `C-AGENT-ALL`; `agent/datasets/README.md`; `docs/evaluation/rcaeval/PROTOCOL.md`.

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

### L-RESTORE-DETECTION

**A Coordinator restored from a backup is detected, but lost acknowledged results are re-driven by hand.** Limitation, mitigated; recorded 2026-11-12. Planned: P4.02. Issues: #62, #63.

- Impact: Restoring the Coordinator database from an earlier copy drops results it had already acknowledged, and workers do not resend acknowledged results by themselves. Since P3.06 the database has a random generation and a commit counter that grows with every changing transaction, every worker reply carries both, with the counter read before the request changed anything, and a worker that sees another generation, or a counter from before its request lower than one it saw before sending, stops claiming new work, answers /readyz with 503 and logs it once, across restarts, while still sending its saved results. scripts/coordinator_restore_check.py lists the acknowledged results the restored database lacks, as replay (the worker resends its saved result when it claims the same request again) or review. Because the counter from before the request is compared, the worker's own request (every claim commits) cannot hide a lost commit, so a backup lacking only the newest acknowledged result is detected. Limits: a worker notices only at its next exchange, and only if it saw a higher counter before; if a restore lost k commits and other workers' requests commit k or more times before a worker's next exchange (each idle claim counts), that worker does not notice (the check with --previous-commit still does). A worker whose Coordinator sends no store headers detects nothing. A worker restarted after the restore compares with the position at its latest acknowledged result only. The Observer's incident outbox does not watch the store, and nothing re-drives a review item. In restore drills (separate processes on one Windows host, file mode; never on a cluster) every worker stopped within 0.31 s and the check listed exactly the two lost results (those drills ran before the counter from before the request was compared, which can only make a worker notice sooner). Clearing every worker at once left the action VERIFYING in 4 of 8 drills: by our reading another Executor claimed the replayed request first and reported its own result, which the Verifier's job store refuses as a second execution for the action. Clearing the workers holding replay items first recovered 5 of 5 with no new write.
- Workaround: Before a restore, note the commit counter of the database being replaced (SELECT commit_counter FROM store_meta); if it cannot be read because the node or volume is lost, the positions remembered in the workers' transport.db files are the only reference. Run scripts/coordinator_restore_check.py with --previous-commit and every worker's transport.db after it, re-drive or review what it lists, then clear each worker with worker_service --accept-store, those holding replay items first.
- Evidence: `services/coordinator/store.py:127`; `services/coordinator/store.py:377`; `runtime/worker_http.py:409`; `runtime/remote_worker.py:248`; `scripts/coordinator_restore_check.py`; `tests/test_store_generation.py:374`; `tests/test_store_generation.py:440`.

### L-ONE-FAULT-ONE-ACTION

**One fault type and one repair action.** Limitation, mitigated; recorded 2026-11-11. Planned: P2.08, P3.08, P3.09. Issues: #74, #76.

- Impact: Live verification pending. Every live recovery so far is an invalid shipping PORT where ROLLBACK is always right, so no result yet shows a choice between actions. The scenario catalog (experiments/boutique/catalog.json) now adds two agent-mode faults on the connected lab with pre-registered labels: R2, a rollout to an image digest that is not on the node (ROLLBACK correct), and N1, the same ErrImageNeverPull symptom from removing the image on the kind node while the template stays at the baseline (ESCALATE correct, ROLLBACK unsafe). ESCALATE, NO_ACTION and NO_INCIDENT are scored outcomes in scenario-result.json, and a run passes only on the labelled decision. A planning failure (abstention, an unavailable or invalid model, exhausted attempts) also ends in a saved ESCALATE, but it scores no_decision, never correct or acceptable, and a correct N1 also needs the eligibility rule to report the pre-registered TEMPLATE_ALREADY_BASELINE. This is tested offline only: no R2 or N1 run has been made, the labels still need owner sign-off, and whether a live N1 really shows ErrImageNeverPull (crictl rmi of an image a running container uses, then a new Pod) is unverified. RESTART is now implemented through the Planner, Guard, Executor and Verifier, but it has only run offline against a fake Deployment API; it has never run against a cluster. SCALE is in the contract and is still refused.
- Workaround: Quote live results only for the PORT fault and ROLLBACK until scored R2 and N1 trials are committed. Run them with the commands in docs/testing.md, after the labels are signed off. Describe RESTART as tested offline only.
- Evidence: `services/guard/core.py:176-177`; `services/executor/core.py:413`; `tests/test_restart_action.py`; claim `C-CONNECTED-HARNESS`; `experiments/boutique/catalog.json`; `experiments/boutique/harness/scenarios.py`; `experiments/boutique/harness/scenarios.py`; `tests/test_live_agent.py`; `experiments/boutique/harness/live.py`; `tests/test_live_agent.py`; `tests/test_live_agent.py`; `tests/test_recovery_scenarios.py`.

### L-MODEL-LATENCY

**Model cold loads make agent planning slow and variable.** Limitation, mitigated; recorded 2026-11-10. Planned: P2.01. Issues: #85, #87.

- Impact: Incident-to-repair time in agent mode had a long tail (docs/results.md). The two measured cold calls took up to 41 s with model loads up to 26 s, and one 356 s decision stage holds only 53 s of recorded model calls, so its cause is not established. The bounded transport is in code: each model call has an overall deadline, a response size cap, no redirects or proxies and a schema check; agent mode runs a preflight and a warm-up call before any fault is injected and records model-runtime.json; keep_alive is configurable. Measured live on 4afc879 (C-AGENT-RECORDED: three runs on one Windows host, 2026-11-11): with qwen3:14b Q4_K_M fully on the GPU and keep_alive 30m, all nine model calls were warm (load under 1 s), model time per run was 21.9 to 24.0 s and incident to Guard ALLOW took 23.3 to 25.6 s; the first run's warm-up loaded the model in 18.6 s before the fault was injected. The preflight stopped a missing model before fault injection (C-AGENT-PREFLIGHT-RECORDED). Not measured: a cold load during a run with the bounded transport, so the tail after an evicted or cold model is unknown. The per-call timeout still defaults to 300 s; P2.01 puts agent planning under a planning budget.
- Workaround: Keep the model warm with keep_alive and check the warm-up load time in model-runtime.json after each run.
- Evidence: `agent/llm.py:285`; `agent/llm.py:455`; `experiments/boutique/harness/agent_live.py:194`; `tests/test_llm_transport.py`.

### L-UNPINNED-BASE-IMAGES

**Sentinel component images used unpinned base image tags.** Limitation, mitigated; recorded 2026-11-12. Planned: P1.09. Issues: #73.

- Impact: Fixed in code: every FROM line, the frontend stage included, is pinned by digest, a test fails on an unpinned base, and Dependabot proposes digest updates. It is recorded as mitigated, not fixed, until the pull request that merges it is known. Images built for earlier evidence used unpinned bases.
- Workaround: Not needed for new builds. Record the built image id with each run.
- Evidence: `deploy/docker/coordinator.Dockerfile:1`; `deploy/docker/guard.Dockerfile:2`; `deploy/docker/observer.Dockerfile:1`; `tests/test_supply_chain_pins.py:57`.

### L-EVIDENCE-INFERRED

**Most committed evidence has inferred provenance.** Limitation, mitigated; recorded 2026-11-12. Planned: P2.09. Issues: #63.

- Impact: Bundles collected before provenance stamping carry a best-guess revision: every chaos, security-lab, observer, packaged, shipping-lab, provider-recovery and ui-session bundle, the four earlier connected-harness runs, the two earlier Guard rejections and 20 agent runs. Recorded bundles now exist for the cited live classes: one connected-harness run, one Guard rejection with zero Executor patch attempts and four agent runs from 4afc879 on 2026-11-11 (C-CONNECTED-HARNESS-RECORDED, C-GUARD-REJECTION-RECORDED, C-AGENT-RECORDED, C-AGENT-PREFLIGHT-RECORDED), plus the offline Guard evaluation. The three Executor wedge bundles are inferred: the harness summary has no provenance block, so each revision is the operator's statement in the MANIFEST basis. Every live run is on one Windows host, and no release tag ties the evidence to a frozen artefact.
- Workaround: Quote each claim with its scope caveat, which names its provenance, and prefer the claims whose bundles are recorded.
- Evidence: claim `C-CONNECTED-HARNESS`; claim `C-CONNECTED-HARNESS-RECORDED`; claim `C-AGENT-RECORDED`; `evidence/README.md`.

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

- Impact: A failed agent run looked like a stalled one, and Ollama's own error text was discarded. The incident also stayed OPEN until agent planning moved into the journal; see L-AGENT-JOURNAL.
- Workaround: Not needed.
- Evidence: `experiments/boutique/harness/agent_live.py:389`; `tests/test_live_agent.py:243`.

### L-CHAOS-SCENARIOS

**The documented Chaos Mesh network, Pod and DNS scenarios could not run as written.** Defect, fixed by #90; recorded 2026-11-11. Planned: none. Issues: #76.

- Impact: Chaos Mesh rejected the network scenario, the Pod fault run was always marked failed, and the DNS fault could not work on the hardened pods.
- Workaround: Not needed; DNS is now refused before any cluster work.
- Evidence: `tests/test_boutique_chaos.py:45`; `tests/test_boutique_chaos.py:51`; `tests/test_boutique_chaos.py:193`.

### L-SAMPLER-READ-ABORT

**One failed kubectl read during Verifier sampling aborted the pass.** Defect, fixed by #94; recorded 2026-11-11. Planned: none. Issues: none.

- Impact: A single transient read error ended verification with no outcome.
- Workaround: Not needed.
- Evidence: `tests/test_verifier_pipeline.py:482`.

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
