# Repository consolidation review — 5 November 2026

## Publication and GitHub status

The tested consolidation is prepared on `overall-integration-cleanup` for review.
`main` remains at the reviewed `b5266ff` until this branch is approved and merged.
The PR decisions and issue statuses below are assessments of this branch, not
claims that GitHub PRs were merged/closed or issue states were changed. Those
remote actions are deferred until the main-branch integration decision. Existing
PRs, issue owners, milestones, dates and branch history remain intact.

## Result and accepted scope

The local connected Boutique experiment integrates the actual Coordinator,
Observer, Guard, restricted Executor and durable Verifier. The default proposal
and demo approval remain harness-authored. Eleven Boutique Deployments run;
fault injection and supported repair target shipping. Actual shipping failure,
downstream cart/checkout failure, conditional repair, crash reconciliation and
independent sustained recovery passed on the consolidated source.

This establishes the tested local workflow, not perfect behavior for arbitrary
faults or production/distributed deployment. Real LLM integration, authenticated
human approval, remote worker deployment and cross-host target ownership remain
open work. The security inspection/scan experiment is available separately and
does not yet run the guarded recovery chain.

Start with [the current demo handbook](boutique-demo-handbook.md),
[connected setup](boutique-connected.md) and [system flow](current-system-demo.md).
The historical exported runner is not this new source; no new runner image or
CVE scan was produced by this consolidation.

## Roles and visibility

| Role | Used by the connected harness | Evidence / limits |
| --- | --- | --- |
| 1 Coordinator | Actual incident/proposal/approval/lifecycle/outbox and result handling | Local durable stores; demo approval is not authenticated human access. |
| 2 Observer | Actual Kubernetes collection, sustained detector, checkpoint/outbox and incident delivery | Shipping incident detection; namespace inventory observes all Pods. Real SDK/OTLP capability separately passed live validation, not Boutique trace instrumentation. |
| 3 Planner | Harness proposal in the default live path; offline proposal validation/provider boundary now present | No real model call or accepted RAG/LLM evaluation. |
| 4 Guard | Actual state, baseline, policy, resource, dependency and approval checks | New evidence revokes queued authorization and requires a report bound to current evidence. Final scored evaluation remains separate. |
| 5 Executor | Restricted conditional restore, durable action/attempt journal, local lock, replay/conflict checks and post-write reconciliation | Actual RBAC/UID/version/race/competing-process tests; no cross-host lease/fencing claim. |
| 6 Verifier | Actual dependency evaluator and durable sustained health/outcome worker | Live shipping gRPC, supporting readiness and real cart/checkout confirmation; APPLIED remains VERIFYING until its outcome. |

The UI shows the stored Sentinel workflow, all eleven Deployment readiness cards,
namespace Pod status/restarts/warnings and shopping measurements at experiment
stages. Freshness, disconnection and unknown measurements remain explicit.
Application logs use `kubectl logs`; complete request tracing/log streaming and
automatic dependency discovery are not implemented. Process activity of host
components is represented by their records, not six Sentinel Kubernetes Pods.

## Consolidation and fixes

- Combined nine relevant branches in an isolated worktree based on main
  `b5266ff`, preserving teammate commits and local untracked presentations.
- Resolved provider/UI launcher conflicts without replacing the default three
  harness scenarios. Provider mode no longer claims two harness recoveries or
  silently runs the harness-only final rejection scenario.
- Bound remote Guard decision/report ingestion and acknowledgment atomically.
  The old transport reproduced a `CoordinatorError` when new dependency evidence
  required its report. Missing/mismatched proof, changed replay and transaction
  failure now have regressions, including an actual local HTTP round trip.
- The normal Boutique Executor path now obtains fresh Role 6 dependency evidence
  under its target lock. A cached CLEAR file cannot bypass a new blocker.
- Reused the existing Pod/event UI panels on the Boutique page instead of adding
  another telemetry stack. The newer Guard worker covers superseded requests
  once: the live workflow retained five necessary reports rather than seven
  duplicate evaluations.
- Retained the useful cached-frontend timestamp fix from `64bdef9`. Added runbook,
  evaluation and workflow test inputs to the future runner build context. The
  unrelated older packaging workflow was not adopted; the image remains deferred.
- Completed HTTPError cleanup without filtering/suppressing ResourceWarning.
- Replaced the contradictory long handbook and old single-service presentation
  text with the current connected flow. One primary source launcher is prominent
  in README; security/provider/packaging variants are clearly optional.

## Validation performed in this audit

| Check | Actual scope and result |
| --- | --- |
| Python | 839 tests passed on Windows/Python 3.15.1 with ResourceWarning enabled; no HTTPError cleanup warnings. Fake Kubernetes/scanner/provider data and local HTTP test services are offline checks. |
| Frontend | 83 tests, TypeScript check and production build passed. Existing non-failing bundle-size warning remains. |
| Contracts / launchers | Six message fixtures, PowerShell parsing, Bash syntax, complete offline-flow demo and dependency-revalidation demo passed. |
| Guard comparison | Fresh 33-case fixture evaluation with 30 repeats per configuration; synthetic preliminary measurement, not final frozen-release or live recovery accuracy. |
| Connected Boutique | Actual kind/Kubernetes and upstream app: all 15 checkpoints passed, two RECOVERED incidents, one REJECTED unnecessary repair, six passing post-action shopping samples. Crash restart made zero additional repair writes. |
| Browser | Installed Edge clicked all three live controls (202); checked 1366/1024/800/390 widths, pause/manual refresh, actual source staleness, API 503/disconnection/reconnection, shopping failures/results and incident drawer. Zero page JS errors; wide tables scroll inside their containers. |
| Observer SDK lab | Actual instrumented services → Collector → Observer → Coordinator: traces/metrics/logs, metric-only failures with ready Pods, read-only RBAC, PVC restart without duplicates and healthy restoration passed. This is not Boutique tracing. |
| Boutique Observer timing | Actual shipping service: 61.61 seconds healthy, first eligible detection at 7.25 seconds, 60.34 seconds further duplicate suppression; manually restored and cleaned. |
| Cleanup | All three dedicated clusters were removed; browser/rehearsal/dashboard/collector/port-forward processes stopped and private test credentials removed. Docker Desktop and image caches remain. |

No actual LLM provider, cloud/Floci, new runner build, fresh CVE scan, protected
security/recovery integration, simultaneous faults or independent-host workers
were exercised in this audit. Security branch's earlier live/scan records remain
historical component evidence, not an additional run performed here.

Ignored evidence under `runs/integration-audit-20261105/`:

- `integrated-python-final.log`, `http-error-cleanup-tests.log`, `browser-validation.json` and screenshots.
- `integration/runs/boutique-live-20261105T095618Z-e58953/` (real connected workflow).
- `integration/runs/observer-otel-20261105T100608Z-a08c28/` (real SDK telemetry).
- `integration/runs/boutique-observer-20261105T100840Z-9895d5/` (real timing windows).
- `integration/runs/guard-evaluation-audit/` (synthetic evaluation).
- Branch/PR/issue inventories, decisions and GitHub mutation receipts.

An initial live launch was refused because Docker Desktop was stopped; no
cluster/recovery success is attributed to that attempt. Docker was started and
the subsequent actual run passed. The first temporary read-only rehearsal missed
tool-path setup; it was stopped and restarted correctly before browser acceptance.

## Open PR decisions

Seven implementation/fix PR heads were included in the consolidation. Four
older/superseded PRs are recommended for closure instead of adding stale binary
screenshots or duplicate acceptance documents. All eleven remain unchanged on
GitHub pending this review branch's acceptance. The retained branches preserve
their original evidence; none is force-reset or deleted.

| PR | Decision | Reason |
| --- | --- | --- |
| [#38](https://github.com/sitwansuey/csc3104_sentinel/pull/38) | Recommend closing as superseded | Superseded by guard-16-17: newer dependency fingerprint/report binding and revocation tests cover more cases than this older patch. |
| [#39](https://github.com/sitwansuey/csc3104_sentinel/pull/39) | Integrated on review branch | Public Coordinator read boundary; existing wire response retained. |
| [#40](https://github.com/sitwansuey/csc3104_sentinel/pull/40) | Integrated on review branch | Offline Planner prerequisite and complete offline flow; harness remains the default live source. |
| [#42](https://github.com/sitwansuey/csc3104_sentinel/pull/42) | Integrated on review branch | Observer OTLP implementation and HTTPError cleanup; independently live-tested. |
| [#43](https://github.com/sitwansuey/csc3104_sentinel/pull/43) | Recommend closing as superseded | Historical screenshots/acceptance-only change for an older dashboard. Replaced by fresh current-code responsive/browser evidence and this single review. |
| [#44](https://github.com/sitwansuey/csc3104_sentinel/pull/44) | Recommend closing as superseded | Read-only rehearsal method was used for fresh actual stale/503/reconnection QA. Historical binary evidence and another shipped server entrypoint are unnecessary; fresh audit supersedes this acceptance-only PR. |
| [#45](https://github.com/sitwansuey/csc3104_sentinel/pull/45) | Integrated on review branch | Timed Boutique Observer acceptance and Windows command decoding fix. |
| [#46](https://github.com/sitwansuey/csc3104_sentinel/pull/46) | Recommend closing as superseded | Historical acceptance summary is superseded by this current role/branch/PR/test/issue review; no production code is lost. |
| [#50](https://github.com/sitwansuey/csc3104_sentinel/pull/50) | Integrated on review branch | Separate repository/project credentials for the Project-status bug; real workflow confirmation remains pending. |
| [#67](https://github.com/sitwansuey/csc3104_sentinel/pull/67) | Integrated on review branch | Optional provider recovery boundary, with conflicts resolved to preserve connected/UI/default rejection scenarios. |
| [#68](https://github.com/sitwansuey/csc3104_sentinel/pull/68) | Integrated on review branch | Optional worker delivery foundation; fixed cross-branch Guard-report handoff before acceptance. |

PR #67 still targets the offline-flow branch. Retarget it to main before merging
this consolidation if GitHub should recognize its included head as merged.
Remote worker delivery is optional foundation code; accepting it does
not complete #18/#19/#62/#63. Old acceptance-only PR closures do not invent the
named API-owner agreement still required by #23/#24. No PR closures have been
published by this audit yet.

## Remote branch review

All 29 current remote branches were checked by ancestry, unique commits and changed
files. Older branch snapshots were not used to overwrite newer team work.

| Branch | Decision | Reason |
| --- | --- | --- |
| `boutique-live-testing` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `buddy2-issue-3-live-detection` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `buddy2-live-k8s-observer` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `buddy2-telemetry-context` | Selected fix only | Telemetry is adopted from the rebased PR #42. Carry over cached-frontend timestamp fix from 64bdef9; defer the older alternative packager/launcher. |
| `chore/project-status-automation` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `feat/11-offline-recovery-flow` | Integrated via Planner branch | Its two feature commits are already included by feat/7-offline-planner. |
| `feat/12-observer-telemetry-evidence` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/13-boutique-observer-acceptance` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/16-dependency-revalidation` | Superseded | Superseded by guard-16-17: newer dependency fingerprint/report binding and revocation tests cover more cases than this older patch. |
| `feat/20-verifier-durable-monitoring` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `feat/21-verifier-live-sources` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `feat/22-dashboard-acceptance-summary` | Superseded acceptance artifacts | Fresh current-code review replaces older screenshots/acceptance summaries; branches retained for historical evidence. |
| `feat/23-coordinator-read-api` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/24-dashboard-responsive-review` | Superseded acceptance artifacts | Fresh current-code review replaces older screenshots/acceptance summaries; branches retained for historical evidence. |
| `feat/25-dashboard-live-rehearsal` | Superseded acceptance artifacts | Fresh current-code review replaces older screenshots/acceptance summaries; branches retained for historical evidence. |
| `feat/30-verifier-live-worker` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `feat/65-coordinator-recovery` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/66-worker-transport` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/7-offline-planner` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `feat/verifier-durable-live-integration` | Keep historical; no wholesale merge | Current services/runtime already contain this Verifier implementation. An old full snapshot would regress newer Guard, UI and Boutique changes; redundant old compatibility tests do not justify it. |
| `feat/verifier-live-overview` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `fix/48-observer-http-response-cleanup` | Integrated via Observer branch | The same cleanup commit is included by feat/12; dashboard response cleanup was completed in this audit. |
| `fix/project-status-repository-token` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `guard-16-17` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |
| `incident_coordinator` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `integration/verifier-20-21` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `main` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `recoveryguard` | Already included / main | No commits ahead of the reviewed main; do not reapply an old snapshot. |
| `security-hardening` | Integrated | Reviewed feature/fix merged with original commit history; combined regressions and scoped live checks passed. |

## Issue assessment

“Not started” means no implementation for that issue's remaining scoped
deliverable was found in the reviewed repository; it does not assert teammates
have no private work. Existing owners, milestones and dates are preserved.
These are review-branch assessments; only eight issues were already closed on
the reviewed main. Ten further scoped issues are implemented and validated on
this branch, awaiting merge before closure. After acceptance, use closed state
for solved issues and `in progress` / `not started` labels for the remaining
backlog. This audit does not reschedule the GitHub Project board or forge
named-person approvals.

Branch assessment: **18 solved, 12 in progress, 20 not started**. These totals describe implementation progress, not current GitHub open/closed counts. Issue #49 remains in progress until a real post-merge Project-status run confirms the credential fix.

| Issue | Assessed status | Evidence / remaining work |
| --- | --- | --- |
| [#3](https://github.com/sitwansuey/csc3104_sentinel/issues/3) [Observer] Test live failure detection | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#4](https://github.com/sitwansuey/csc3104_sentinel/issues/4) [Coordinator] Verify incident persistence and approvals | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#6](https://github.com/sitwansuey/csc3104_sentinel/issues/6) [Guard] Verify validation and Coordinator handoff | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#7](https://github.com/sitwansuey/csc3104_sentinel/issues/7) [Planner] Generate offline rollback proposals | Solved | Offline runbook-guided proposal validation and escalation, with key-free tests; not a real LLM. |
| [#8](https://github.com/sitwansuey/csc3104_sentinel/issues/8) [Executor] Implement offline rollback execution | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#9](https://github.com/sitwansuey/csc3104_sentinel/issues/9) [Verifier] Implement offline dependency and health checks | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#10](https://github.com/sitwansuey/csc3104_sentinel/issues/10) [Coordinator] Implement trusted approval access | Not started | No authenticated operator approval boundary is implemented; the local harness records trusted demo approval. |
| [#11](https://github.com/sitwansuey/csc3104_sentinel/issues/11) [Coordinator] Connect the complete offline recovery flow | Solved | Complete offline component flow, restart/replay and failure regressions run successfully. |
| [#12](https://github.com/sitwansuey/csc3104_sentinel/issues/12) [Observer] Add live OpenTelemetry metric evidence | Solved | Real SDK/Collector/Observer telemetry, metric-only failures, read-only RBAC and PVC restart passed in the new audit run. |
| [#13](https://github.com/sitwansuey/csc3104_sentinel/issues/13) [Observer] Validate detection on Online Boutique | Solved | Fresh Boutique timing: 61.61 seconds healthy, first eligible detection at 7.25 seconds, 60.34 seconds duplicate suppression; restored and cleaned. |
| [#14](https://github.com/sitwansuey/csc3104_sentinel/issues/14) [Planner] Process Coordinator planning requests durably | In progress | Basic transactional Planner handoff exists; bounded provider/retrieval retry and terminal escalation guarantees remain incomplete. |
| [#15](https://github.com/sitwansuey/csc3104_sentinel/issues/15) [Planner] Add a retrieval-backed LLM provider | In progress | Offline retrieval/proposal validation and provider hook exist; no accepted real retrieval-backed LLM adapter/run. |
| [#16](https://github.com/sitwansuey/csc3104_sentinel/issues/16) [Guard] Integrate fresh Verifier dependency assessments | Solved | New Guard branch revokes stale queued authorization and binds revalidation to current Verifier evidence and a persisted report. |
| [#17](https://github.com/sitwansuey/csc3104_sentinel/issues/17) [Guard] Measure validation acceptance and rejection accuracy | In progress | Versioned cases, comparison runner and preliminary synthetic results exist; final scored frozen-release acceptance still depends on #63. |
| [#18](https://github.com/sitwansuey/csc3104_sentinel/issues/18) [Executor] Add coordination across worker hosts | Not started | Current target locks/journals coordinate one host; no shared target ownership or independent-host fencing implementation. |
| [#19](https://github.com/sitwansuey/csc3104_sentinel/issues/19) [Executor] Package a restricted Kubernetes worker | Not started | The general demo runner is not the independently deployed restricted remote Executor worker required by this issue. |
| [#20](https://github.com/sitwansuey/csc3104_sentinel/issues/20) [Verifier] Process recovery verification requests durably | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#21](https://github.com/sitwansuey/csc3104_sentinel/issues/21) [Verifier] Collect live dependency and service health evidence | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#22](https://github.com/sitwansuey/csc3104_sentinel/issues/22) [Dashboard] Build interactive cluster and incident dashboard | In progress | Dashboard implementation and fresh technical/live checks pass; parent remains open for the explicit API owner agreement in #23/#24. |
| [#23](https://github.com/sitwansuey/csc3104_sentinel/issues/23) [Dashboard] Provide read-only cluster and incident API | In progress | Public bounded read interface and API/UI compatibility pass. The explicitly requested named-owner API agreement is not recorded by this audit. |
| [#24](https://github.com/sitwansuey/csc3104_sentinel/issues/24) [Dashboard] Build interactive frontend for the presentation | In progress | All requested responsive/interaction checks pass, including 1366/1024/800 and mobile. Explicit API-owner agreement remains unchecked. |
| [#25](https://github.com/sitwansuey/csc3104_sentinel/issues/25) [Dashboard] Integrate and rehearse the live incident demo | Solved | Fresh connected browser run covered real incidents, four layouts, manual refresh, actual source staleness/API 503/reconnection and cleanup. |
| [#30](https://github.com/sitwansuey/csc3104_sentinel/issues/30) [Verifier] Integrate durable verification with live readings | Solved | Previously accepted and closed; preserved. Relevant existing regressions continue to pass. |
| [#48](https://github.com/sitwansuey/csc3104_sentinel/issues/48) [Tests] Close HTTP error responses to remove ResourceWarning noise | Solved | Observer and dashboard HTTPError responses are closed; 839 tests passed with ResourceWarning enabled and no HTTPError cleanup warnings. |
| [#49](https://github.com/sitwansuey/csc3104_sentinel/issues/49) [Automation] Fix Project status 404 on feature branch pushes | In progress | Credential-routing fix is integrated and tested; a real post-merge Project status workflow must succeed before closing this issue. |
| [#51](https://github.com/sitwansuey/csc3104_sentinel/issues/51) [Integration] Validate the complete live AI-assisted recovery flow | In progress | Actual local harness recovery and provider boundary work; the required real LLM recovery/acceptance remains pending. |
| [#52](https://github.com/sitwansuey/csc3104_sentinel/issues/52) [Planner] Build bounded retrieval context for recovery proposals | In progress | Offline bounded runbook retrieval exists; final provider context/corpus and held-out-evaluation safeguards need completion/review. |
| [#53](https://github.com/sitwansuey/csc3104_sentinel/issues/53) [Planner] Add a bounded LLM transport and validated response adapter | Not started | A trusted subprocess hook is scaffolding, not an implemented bounded real LLM transport/response adapter. |
| [#54](https://github.com/sitwansuey/csc3104_sentinel/issues/54) [Planner] Validate a real provider through durable planning | Not started | No actual-provider planning acceptance evidence was supplied or generated. |
| [#55](https://github.com/sitwansuey/csc3104_sentinel/issues/55) [Integration] Wire the live recovery runner and failure regressions | In progress | Runner and key-free failure/restart wiring are integrated. Final acceptance still depends on #14 and provider prerequisites; do not call it live AI. |
| [#56](https://github.com/sitwansuey/csc3104_sentinel/issues/56) [Integration] Rehearse and accept the real AI-assisted Kubernetes recovery | Not started | No actual LLM/Kubernetes recovery rehearsal/sign-off yet; current live tests deliberately use harness proposals. |
| [#57](https://github.com/sitwansuey/csc3104_sentinel/issues/57) [Runtime] Provide one durable Coordinator transport for service workers | Solved | One optional authenticated durable HTTP delivery boundary is documented/tested, including atomic Guard report/result handoff. Deployment is separate. |
| [#58](https://github.com/sitwansuey/csc3104_sentinel/issues/58) [Executor] Implement shared target ownership and ownership-loss checks | Not started | No shared acquire/renew/expire/loss/fencing mechanism; delivery leases do not provide repair ownership. |
| [#59](https://github.com/sitwansuey/csc3104_sentinel/issues/59) [Executor] Validate independent workers and ownership expiry | Not started | Independent packaged workers/ownership-expiry proof have not been implemented or exercised. |
| [#60](https://github.com/sitwansuey/csc3104_sentinel/issues/60) [Executor] Package the remote worker with persistent state and probes | Not started | No remote Executor-specific entrypoint/image/persistent shared-ownership deployment has been delivered. |
| [#61](https://github.com/sitwansuey/csc3104_sentinel/issues/61) [Executor] Accept deployed RBAC and post-write restart recovery | Not started | The current real RBAC/crash proof is local harness scope; independent deployed-worker acceptance is pending #59/#60. |
| [#62](https://github.com/sitwansuey/csc3104_sentinel/issues/62) [Deployment] Deploy Sentinel service boundaries and remote worker adapters | Not started | Existing component artifacts are prerequisites; selected remote worker bindings and whole-platform deployment are not delivered. |
| [#63](https://github.com/sitwansuey/csc3104_sentinel/issues/63) [Integration] Accept distributed recovery and freeze the evaluated release | Not started | Distributed real-provider release acceptance cannot start before the component/deployment prerequisites are accepted. |
| [#65](https://github.com/sitwansuey/csc3104_sentinel/issues/65) [Coordinator] Route detected incidents through the recovery runner | Solved | Coordinator runner/provider boundary is integrated with failure/restart regressions. Actual model acceptance remains #54/#56. |
| [#66](https://github.com/sitwansuey/csc3104_sentinel/issues/66) [Coordinator] Expose durable authenticated worker delivery | Solved | Role-scoped claim/result/retry/ingress, leases, replay and atomic acknowledgments are implemented; local HTTP Guard proof handoff now works. |
| [#69](https://github.com/sitwansuey/csc3104_sentinel/issues/69) [Integration] Combine protected Boutique shipping with guarded recovery | Not started | Security overlay is now available as an opt-in separate lab; protected templates/probes are not wired into guarded recovery. |
| [#70](https://github.com/sitwansuey/csc3104_sentinel/issues/70) [Security] Add certificate lifecycle and workload identity checks | Not started | Two-day lab certificates exist; rotation, renewal and revocation lifecycle is not implemented. |
| [#71](https://github.com/sitwansuey/csc3104_sentinel/issues/71) [Security] Bound inspection capacity and test gateway availability | Not started | Existing request bounds/fail-closed checks are a baseline; explicit capacity/readiness/availability improvement is not implemented. |
| [#72](https://github.com/sitwansuey/csc3104_sentinel/issues/72) [Security] Reconcile live image inventory and scan freshness | Not started | The monitor rescans a deployment-time inventory; current-image discovery and per-image freshness policy are not implemented. |
| [#73](https://github.com/sitwansuey/csc3104_sentinel/issues/73) [Release] Triage workload CVEs and build a scanned reproducible demo package | In progress | Security build/scan sources and candidate tooling exist; the latest consolidated runner has not been rebuilt, scanned or exported. |
| [#74](https://github.com/sitwansuey/csc3104_sentinel/issues/74) [Workloads] Add a second configured Boutique fault and recovery target | Not started | Only shipping is an allowed fault/repair target; a second service scenario is not implemented. |
| [#75](https://github.com/sitwansuey/csc3104_sentinel/issues/75) [Evaluation] Select a published Boutique benchmark and freeze Planner scoring | Not started | The intended published dataset is still unconfirmed; the agreed benchmark/scoring protocol is not established. |
| [#76](https://github.com/sitwansuey/csc3104_sentinel/issues/76) [Experiments] Add opt-in Chaos Mesh faults with scoped cleanup | Not started | Chaos Mesh has not been installed or integrated; optional after the core flow. |
| [#77](https://github.com/sitwansuey/csc3104_sentinel/issues/77) [Experiments] Evaluate two simultaneous faults and dependency-safe recovery | Not started | There is no accepted two-target simultaneous-fault experiment; optional after individual scenarios. |
| [#78](https://github.com/sitwansuey/csc3104_sentinel/issues/78) [Roadmap] Complete connected Boutique AI recovery and staged improvements | In progress | This audit consolidates core local work and the backlog. Real Planner/protected recovery and optional expansions remain open. |

## Immediate next work

Keep the tested local harness flow as the regression baseline. Complete the real
Planner path (#14/#15/#52-#54, then #55/#56/#51) and record actual model provenance
and independent shopping recovery. In parallel the two API owners can record
the response-contract agreement for #23/#24/#22. Protected recovery (#69),
production approval/identity, remote deployment/ownership and release packaging
remain distinct deliverables. Second-service repair, dataset scoring, Chaos Mesh
and simultaneous faults follow their existing scoped issues rather than being
silently added to the working local demo.
