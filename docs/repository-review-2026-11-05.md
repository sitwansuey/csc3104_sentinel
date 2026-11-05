# Repository consolidation and cleanup — 5 November 2026

## Current checkpoint

The original consolidation was merged to `main` through PR #79 (`24a060c`).
All eleven originally open PRs are resolved: six were recognized as merged,
one was closed as already included, and four were closed as superseded.
GitHub now has **20 closed issues, 10 in progress and 20 not started**.
Remaining issue ownership and milestone assignments were preserved; Project
dates/manual statuses were not rescheduled.

The subsequent storage/documentation cleanup is prepared on
`overall-integration-cleanup` for review before its next main merge. It passed
the full 839-test offline suite. **Live Kubernetes tests below refer to the
earlier consolidation, before this storage refactor.** Another connected run and
the next image build/scan remain pending.

The supported local flow uses actual Roles 1, 2, 4, 5 and 6 with harness-authored
proposals/demo approval. Eleven Boutique Deployments are observed; fault
injection and repair target shipping. This is the tested local workflow, not
arbitrary-fault or production/distributed acceptance. Start with the
[demo handbook](boutique-demo-handbook.md) and [documentation map](README.md).

During the final fetch a new branch,
`integrated-agentic-harness-and-simplified-workflow` (`87a63e3`), appeared. It adds
an RCA/AI agent and moves many service, deployment and experiment files. It is
not on the reviewed main and was not included or accepted in this cleanup. Its
implementation/evaluation claims and overlapping file moves need a separate
review before merging. Issue counts below assess the merged main, not acceptance
of that newly published work.

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

## Changes retained from the branch review

| Useful branch | Contribution |
| --- | --- |
| `feat/12-observer-telemetry-evidence` | OTLP evidence, metric-only detection, response cleanup |
| `feat/13-boutique-observer-acceptance` | Timed real Boutique acceptance and Windows decoding |
| `feat/23-coordinator-read-api` | Bounded public Coordinator read interface |
| `feat/7-offline-planner` | Offline retrieval/proposal validation and complete offline flow |
| `feat/65-coordinator-recovery` | Optional provider recovery boundary and restart handling |
| `feat/66-worker-transport` | Optional authenticated durable delivery foundation |
| `guard-16-17` | Fresh dependency revocation/report binding and preliminary evaluation |
| `security-hardening` | Separate protected Boutique/mTLS inspection and CVE monitor lab |
| `fix/project-status-repository-token` | Separate repository/Project credential routing |

Additional integration fixes bound remote Guard report/result/acknowledgment in
one transaction, refreshed Executor dependency evidence under its workload lock,
preserved both default UI scenarios and optional provider mode, exposed Pods and
warning events on the Boutique page, completed HTTPError cleanup and repaired
cached frontend/build-context handling. The older alternative packager was not
adopted. This retained teammate commit history rather than replacing their work.

All 29 original remote branches were assessed. The nine above were combined;
`feat/11-offline-recovery-flow` and `fix/48-observer-http-response-cleanup` were
already included through those branches. `buddy2-telemetry-context` contributed
only its cached-frontend timestamp fix (`64bdef9`); its telemetry was adopted
through the newer Observer branch. `feat/verifier-durable-live-integration`
already has its services/runtime on main; its old complete snapshot was not
used to overwrite newer code. The four superseded branches correspond to
#38/#43/#44/#46 below. The other twelve branches were already ancestors of main:

`boutique-live-testing`, `buddy2-issue-3-live-detection`, `buddy2-live-k8s-observer`, `chore/project-status-automation`, `feat/verifier-live-overview`, `feat/20-verifier-durable-monitoring`, `feat/21-verifier-live-sources`, `feat/30-verifier-live-worker`, `incident_coordinator`, `integration/verifier-20-21`, `main`, `recoveryguard`.

## Cleanup and code boundaries

- `services/coordinator.py` now contains lifecycle/approval/routing decisions.
  `services/coordinator_store.py` owns the SQLite schema, record queries, outbox
  operations and transaction context. Existing `services.coordinator` imports of
  `SQLiteStore`/`CoordinatorError` remain compatible.
- Guard, Planner, Executor and Verifier pipeline workers use named storage
  operations. The same `BEGIN IMMEDIATE` boundaries still commit results and
  acknowledgments together. Kubernetes calls stay outside the Executor's
  Coordinator transaction. No ORM or schema migration was added.
- `services/executor_delivery_store.py` owns delivery-conflict audit-table SQL.
  Actual execution decisions remain in `services/executor.py`, the execution
  journal in `services/executor_store.py`, and Kubernetes operations in
  `services/k8s_executor_adapter.py`. Separate ledgers/Kubernetes still require
  crash reconciliation.
- Consolidated `laptop-demo-quickstart.md` into `packaged-demo.md` and the stale
  `guard-role.md` into `recovery-guard.md`. Updated links, API/packaging limits
  and storage maps. `docs/README.md` is the navigation index.
- Kept offline fixtures, role demos, specialised live scripts and audit evidence:
  they test distinct boundaries and are not unused simulation code. Local
  presentation files were left untouched and excluded from commits.

Post-cleanup checks: 839 Python tests with ResourceWarning enabled passed;
reopening a copy of the earlier actual live-run Coordinator database preserved
all public records (three incidents/three actions). This compatibility check is
not a new Kubernetes recovery test. Current frontend source is unchanged from
the earlier 83-test/build acceptance.

## PR outcomes

| PR | Actual outcome | Reason |
| --- | --- | --- |
| #39, #40, #42, #45, #50, #68 | Merged through #79 | Included implementations retained with original commit history |
| #67 | Closed as already integrated | Head `7d2bbe9` is an ancestor of main. GitHub rejected base retargeting with HTTP 422; no duplicate merge was made. This is completion, not rejection of the implementation. |
| #38 | Closed as superseded | Newer Guard branch binds the exact dependency finding/report and covers revocation more fully |
| #43 | Closed as superseded | Old responsive screenshots/acceptance replaced by fresh current-code browser validation |
| #44 | Closed as superseded | Rehearsal method was used for fresh stale/503/reconnect QA; old binary evidence/additional server entrypoint not shipped |
| #46 | Closed as superseded | Older acceptance summary replaced by this consolidated review |

Branches/history were not deleted or force-reset. No named API-owner sign-off was
invented: #23 was closed by its accepted merge, while #24/#22 retain the explicit
frontend/API agreement work. #49 was also closed by its merge; main Project status
passed. Feature-branch comparison acceptance is tracked in that issue's workflow
evidence rather than inferred from offline tests.

## Issue status after housekeeping

GitHub issue comments hold the individual reasons; status labels distinguish
partial implementation from remaining deliverables. “Not started” means no
implementation for the remaining issue scope was found in this repository; it
does not assert that teammates have no private work.

**Closed / completed (20):** [#3](https://github.com/sitwansuey/csc3104_sentinel/issues/3), [#4](https://github.com/sitwansuey/csc3104_sentinel/issues/4), [#6](https://github.com/sitwansuey/csc3104_sentinel/issues/6), [#7](https://github.com/sitwansuey/csc3104_sentinel/issues/7), [#8](https://github.com/sitwansuey/csc3104_sentinel/issues/8), [#9](https://github.com/sitwansuey/csc3104_sentinel/issues/9), [#11](https://github.com/sitwansuey/csc3104_sentinel/issues/11), [#12](https://github.com/sitwansuey/csc3104_sentinel/issues/12), [#13](https://github.com/sitwansuey/csc3104_sentinel/issues/13), [#16](https://github.com/sitwansuey/csc3104_sentinel/issues/16), [#20](https://github.com/sitwansuey/csc3104_sentinel/issues/20), [#21](https://github.com/sitwansuey/csc3104_sentinel/issues/21), [#23](https://github.com/sitwansuey/csc3104_sentinel/issues/23), [#25](https://github.com/sitwansuey/csc3104_sentinel/issues/25), [#30](https://github.com/sitwansuey/csc3104_sentinel/issues/30), [#48](https://github.com/sitwansuey/csc3104_sentinel/issues/48), [#49](https://github.com/sitwansuey/csc3104_sentinel/issues/49), [#57](https://github.com/sitwansuey/csc3104_sentinel/issues/57), [#65](https://github.com/sitwansuey/csc3104_sentinel/issues/65), [#66](https://github.com/sitwansuey/csc3104_sentinel/issues/66).

**In progress (10):** [#14](https://github.com/sitwansuey/csc3104_sentinel/issues/14), [#15](https://github.com/sitwansuey/csc3104_sentinel/issues/15), [#17](https://github.com/sitwansuey/csc3104_sentinel/issues/17), [#22](https://github.com/sitwansuey/csc3104_sentinel/issues/22), [#24](https://github.com/sitwansuey/csc3104_sentinel/issues/24), [#51](https://github.com/sitwansuey/csc3104_sentinel/issues/51), [#52](https://github.com/sitwansuey/csc3104_sentinel/issues/52), [#55](https://github.com/sitwansuey/csc3104_sentinel/issues/55), [#73](https://github.com/sitwansuey/csc3104_sentinel/issues/73), [#78](https://github.com/sitwansuey/csc3104_sentinel/issues/78).

**Not started (20):** [#10](https://github.com/sitwansuey/csc3104_sentinel/issues/10), [#18](https://github.com/sitwansuey/csc3104_sentinel/issues/18), [#19](https://github.com/sitwansuey/csc3104_sentinel/issues/19), [#53](https://github.com/sitwansuey/csc3104_sentinel/issues/53), [#54](https://github.com/sitwansuey/csc3104_sentinel/issues/54), [#56](https://github.com/sitwansuey/csc3104_sentinel/issues/56), [#58](https://github.com/sitwansuey/csc3104_sentinel/issues/58), [#59](https://github.com/sitwansuey/csc3104_sentinel/issues/59), [#60](https://github.com/sitwansuey/csc3104_sentinel/issues/60), [#61](https://github.com/sitwansuey/csc3104_sentinel/issues/61), [#62](https://github.com/sitwansuey/csc3104_sentinel/issues/62), [#63](https://github.com/sitwansuey/csc3104_sentinel/issues/63), [#69](https://github.com/sitwansuey/csc3104_sentinel/issues/69), [#70](https://github.com/sitwansuey/csc3104_sentinel/issues/70), [#71](https://github.com/sitwansuey/csc3104_sentinel/issues/71), [#72](https://github.com/sitwansuey/csc3104_sentinel/issues/72), [#74](https://github.com/sitwansuey/csc3104_sentinel/issues/74), [#75](https://github.com/sitwansuey/csc3104_sentinel/issues/75), [#76](https://github.com/sitwansuey/csc3104_sentinel/issues/76), [#77](https://github.com/sitwansuey/csc3104_sentinel/issues/77).

Seven additional completed scoped issues were closed during this pass:
#11, #16, #25, #48, #57, #65 and #66. Future improvement issues were kept open.
Offline Planner/transport foundations do not complete real LLM or distributed
deployment acceptance.

Priorities remain: real Planner/provider retry and acceptance (#14/#15/#52-#56),
authenticated approval (#10), protected recovery (#69), cross-host ownership and
deployed workers (#18/#19/#58-#63), and a newly built/scanned package (#73).
Dataset choice (#75), a second repair target (#74), Chaos Mesh (#76) and
simultaneous faults (#77) remain optional staged experiments.

## Earlier consolidation validation

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

## Local Docker cleanup

There were no running or stopped containers and no Docker volumes to remove.
Removed these six unused Sentinel image references without force:

- `sentinel-boutique-runner:0.2.0`
- `sentinel-boutique-runner:0.3.0`
- `sentinel-demo:0.1.0`
- `sentinel-boutique-shipping:0.10.7-arm64-r2`
- `sentinel-observer:0.3.0`
- `sentinel-otel-validation:0.1.0`

Retained seventeen upstream/base/tool images required by current labs or other
builds. Docker's reported image usage fell from 3.875 GB to 3.32 GB. Shared build
cache (13.35 GB), scan/checksum evidence and Docker Desktop were retained;
there was no global image/container/volume/cache prune. The obsolete 0.3.0
exported runner TAR and matching source ZIP were removed too, so the old bundle
cannot accidentally be shared as the current release. Its historical scan and
checksum records remain. No replacement image or new CVE scan was produced.

Ignored `runs/cleanup-20261105/` contains GitHub inventories/action receipts,
Docker before/after inventories, the post-refactor regression log and compatibility
check evidence. The earlier consolidation's three dedicated test clusters were
already removed. The next step is a fresh connected experiment on this cleanup
source, followed by the separately planned image build and scan.
