# Results

Every table on this page is generated from the bundles in [evidence/](../evidence/README.md)
by `python scripts/evidence/summarize.py`. Do not edit the tables by hand: CI
runs `summarize.py --check` and fails on any difference. Prose outside the
tables is free text.

Read each number with its limits:

- Runs are on one Windows/amd64 host with Docker Desktop and kind. Each table
  counts runs, not independent trials; repeated runs share the host and model.
- Failed, interrupted and incomplete runs are counted in every denominator.
- `inferred` provenance means the run files carry no provenance block (most
  predate provenance stamping), so the source revision is a best guess or an
  operator statement; each bundle's MANIFEST `basis` says which. Only
  `recorded` bundles prove a commit.
- The recovery scenario is one fault type (an invalid shipping PORT) where the
  correct action is always ROLLBACK, and lab approval is pre-authorised.

## Runs by class

<!-- BEGIN GENERATED:runs -->
| Class | Runs | PASS | FAILED | INTERRUPTED | INCOMPLETE | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| agent | 24 | 14 | 10 | 0 | 0 | 4 recorded, 20 inferred |
| chaos-batch | 17 | 3 | 13 | 0 | 1 | 0 recorded, 17 inferred |
| connected-harness | 5 | 5 | 0 | 0 | 0 | 1 recorded, 4 inferred |
| executor-wedge | 3 | 2 | 1 | 0 | 0 | 0 recorded, 3 inferred |
| guard-eval | 1 | 1 | 0 | 0 | 0 | 1 recorded, 0 inferred |
| guard-rejection | 3 | 3 | 0 | 0 | 0 | 1 recorded, 2 inferred |
| observer-acceptance | 3 | 3 | 0 | 0 | 0 | 0 recorded, 3 inferred |
| packaged | 9 | 5 | 0 | 1 | 3 | 0 recorded, 9 inferred |
| provider-recovery | 1 | 0 | 1 | 0 | 0 | 0 recorded, 1 inferred |
| security-lab | 3 | 3 | 0 | 0 | 0 | 0 recorded, 3 inferred |
| shipping-lab | 2 | 2 | 0 | 0 | 0 | 0 recorded, 2 inferred |
| ui-session | 7 | 0 | 0 | 0 | 7 | 0 recorded, 7 inferred |
<!-- END GENERATED:runs -->

## Recovery checks

Each row is a check from the harness `summary.json`. A run that stopped early
counts as not passed for the checks it never reached.

<!-- BEGIN GENERATED:recovery-checks -->
| Class | Check | Passed / runs |
| --- | --- | --- |
| connected-harness | healthy baseline captured with real service samples and immutable image | 5/5 |
| connected-harness | real ServiceAccount permits only selected Deployment get/patch; forbidden write denied | 5/5 |
| connected-harness | Connected cart and checkout fail while shipping configuration is broken | 5/5 |
| connected-harness | missing-approval makes zero executor writes | 5/5 |
| connected-harness | hard-policy-rejection makes zero executor writes | 5/5 |
| connected-harness | stale-state makes zero executor writes | 5/5 |
| connected-harness | real API rejects JSON Patch after resourceVersion changes | 5/5 |
| connected-harness | competing real worker is excluded before mutation | 5/5 |
| connected-harness | approved real rollback, duplicate replay and changed-ID rejection | 5/5 |
| connected-harness | Role 6 verified gRPC, readiness and template; Coordinator RECOVERED | 5/5 |
| connected-harness | gRPC quote and replica health after executor rollback; Role 6 published RECOVERED | 5/5 |
| connected-harness | actual worker process exits after live patch; restart reconciles without resubmission | 5/5 |
| connected-harness | Guard rejects unnecessary rollback despite approval; zero Executor patch attempts | 5/5 |
| shipping-lab | healthy baseline captured with real service samples and immutable image | 2/2 |
| shipping-lab | real ServiceAccount permits only selected Deployment get/patch; forbidden write denied | 2/2 |
| shipping-lab | missing-approval makes zero executor writes | 2/2 |
| shipping-lab | hard-policy-rejection makes zero executor writes | 2/2 |
| shipping-lab | stale-state makes zero executor writes | 2/2 |
| shipping-lab | real API rejects JSON Patch after resourceVersion changes | 2/2 |
| shipping-lab | competing real worker is excluded before mutation | 2/2 |
| shipping-lab | approved real rollback, duplicate replay and changed-ID rejection | 2/2 |
| shipping-lab | Role 6 verified gRPC, readiness and template; Coordinator RECOVERED | 2/2 |
| shipping-lab | gRPC quote and replica health after executor rollback; Role 6 published RECOVERED | 2/2 |
| shipping-lab | actual worker process exits after live patch; restart reconciles without resubmission | 2/2 |
| shipping-lab | Guard rejects unnecessary rollback despite approval; zero Executor patch attempts | 2/2 |
| packaged | healthy baseline captured with real service samples and immutable image | 5/9 |
| packaged | real ServiceAccount permits only selected Deployment get/patch; forbidden write denied | 5/9 |
| packaged | missing-approval makes zero executor writes | 5/9 |
| packaged | hard-policy-rejection makes zero executor writes | 5/9 |
| packaged | stale-state makes zero executor writes | 5/9 |
| packaged | real API rejects JSON Patch after resourceVersion changes | 5/9 |
| packaged | competing real worker is excluded before mutation | 5/9 |
| packaged | approved real rollback, duplicate replay and changed-ID rejection | 5/9 |
| packaged | Role 6 verified gRPC, readiness and template; Coordinator RECOVERED | 5/9 |
| packaged | gRPC quote and replica health after executor rollback; Role 6 published RECOVERED | 5/9 |
| packaged | actual worker process exits after live patch; restart reconciles without resubmission | 5/9 |
| packaged | Guard rejects unnecessary rollback despite approval; zero Executor patch attempts | 5/9 |
<!-- END GENERATED:recovery-checks -->

## Agent mode

Grouped by source revision. Model seconds are the summed wall time of the
model calls in one run.

<!-- BEGIN GENERATED:agent-runs -->
| Revision | Provenance | Runs | RECOVERED | Not recovered | Model calls (median) | Model seconds (median) | Failure classes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 5559eb0 | inferred | 4 | 2/4 | 2 | 4.5 | 73.75 | not recovered x2 |
| d013ea5 | inferred | 5 | 3/5 | 2 | 4 | 45.9 | model not installed x1; model server unreachable x1 |
| 4e8b44a | inferred | 3 | 1/3 | 2 | 4 | 66.4 | model not installed x1; model server unreachable x1 |
| 07a9771 | inferred | 1 | 0/1 | 1 | n/a | n/a | model server unreachable x1 |
| 6cbb32f | inferred | 4 | 2/4 | 2 | 3 | 53.6 | agent escalated x1; model not installed x1 |
| b5e8bad | inferred | 3 | 3/3 | 0 | 3 | 26.4 | none |
| 4afc879 | recorded | 4 | 3/4 | 1 | 3 | 23.6 | model not installed x1 |
<!-- END GENERATED:agent-runs -->

## Guard rejection

A healthy service receives an approved but unnecessary rollback. Guard must
refuse it and the Executor must make no write.

<!-- BEGIN GENERATED:guard-rejection -->
| Runs | Refused | Executor writes for the refused action | Unmeasured |
| --- | --- | --- | --- |
| 3 | 3/3 | 0 | 0 |
<!-- END GENERATED:guard-rejection -->

## Guard fixture evaluation

`scripts/evaluate_guard.py` runs the versioned synthetic Guard cases offline:
no cluster, no model, no repair. `full_available` and `state_policy_only` are
measured. `no_validation` is definitional: it allows every proposal by
construction and is the anchor for acting on a proposal with no checks. The
status stays `PRELIMINARY_NOT_FROZEN` until a release manifest accepted under
issue #63 names the evaluated clean commit. These are hand-authored fixtures,
not live incidents. See [the suite README](../data/experimentation/guard/README.md).

<!-- BEGIN GENERATED:guard-eval -->
| Bundle | Status | Configuration | Basis | Unsafe ALLOW | Unsafe REJECT | Unsafe ESCALATE | Safe ALLOW | False REJECT | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | state_policy_only | measured | 21/27 | 6/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | full_available | measured | 0/27 | 18/27 | 9/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 | 0/27 | 0/27 | 6/6 | 0/6 | 0 |
<!-- END GENERATED:guard-eval -->

## Executor wedge

`scripts/validation/test_executor_wedge_live.py` drives one Executor action
into an in-doubt write in a disposable namespace on the local kind cluster,
then runs five reconcile passes and, if the workload is released, a second
proposal. `rv-race` moves the resourceVersion before the patch; `delete-recreate`
replaces the Deployment after the write is journaled. A FAILED row can be the
expected "before" result: it records the harness run against an Executor that
predates the reconcile fix. The scenario summaries carry no provenance block,
so the revision is `inferred` and each bundle's MANIFEST `basis` says how it
is known. Guard is not under test, and the same-UID case where the
resourceVersion does not move is not covered.

<!-- BEGIN GENERATED:executor-wedge -->
| Bundle | Scenario | Revision | Provenance | Outcome | Reconcile result | Reconcile passes | Second proposal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| executor-wedge-rv-race-20261111t202123z | rv-race | 4afc879 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-delete-recreate-20261111t202124z | delete-recreate | 4afc879 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-rv-race-before-20261111t202149z | rv-race | d5b2ed9 | inferred | FAILED | NEEDS_RECONCILIATION | 5/5 NEEDS_RECONCILIATION | not admitted |
<!-- END GENERATED:executor-wedge -->

## Timings

Successful runs only. `recorded` values come from the harness timeline;
`approx` values come from file timestamps in older bundles.

<!-- BEGIN GENERATED:timings -->
| Class | Interval | n | Median s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- |
| agent | fault -> incident observed | 3 | 7.8 | 7.6 | 7.8 | recorded |
| agent | incident observed -> repair applied | 14 | 45.75 | 23.7 | 356.5 | approx |
| agent | repair applied -> outcome window end | 14 | 43.7 | 26.5 | 61.5 | approx |
| connected-harness | fault -> incident observed | 1 | 7.7 | 7.7 | 7.7 | recorded |
| connected-harness | incident observed -> repair applied | 5 | 3.4 | 3.4 | 3.8 | approx |
| connected-harness | repair applied -> outcome window end | 5 | 26.4 | 26.1 | 55.5 | approx |
<!-- END GENERATED:timings -->

## Per-stage latency

Generated from the newest ledger in `evidence/evaluation/latency/`, which
`python scripts/latency_report.py` builds from the agent and connected-harness
bundles. Each row is one stage over the runs that reached it, so `n` differs
by stage and failed runs stay in the run counts. p90 is nearest rank. Read it
with these limits:

- Runs before 11 November 20:00 UTC did not record their injection time, so
  their detection starts at the first failing Observer snapshot, a lower
  bound. The four runs at 4afc879 record `fault_injected_at`; the Source
  column counts each start basis.
- Agent-mode planning and Guard need `planning-progress.jsonl`, which bundles
  do not keep. Those rows come from the local run folders (`run folder`) and
  cannot be rechecked in CI. `summarize.py --check` recomputes every `bundle`
  value.
- Guard here is wall time from the last planning record to the ALLOW verdict.
  It includes the dependency check and the lab's pre-authorised approval, so
  there is no human wait in it. In harness mode, "incident -> Guard ALLOW"
  also covers the scripted negative cases that run first.
- One fault type on one host, one model (`qwen3:14b`) and runs from several
  revisions. These are not independent trials.

<!-- BEGIN GENERATED:latency -->
- agent: 24 runs, 14 PASS; stopped at: planning x8; verification x2.
- harness: 5 runs, 5 PASS; stopped at: none.

| Mode | Stage | n | Median s | p90 s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| agent | detection: start -> incident observed | 23 | 7.2 | 7.4 | 5.0 | 7.4 | bundle; start: first failing snapshot x20, injection x3 |
| agent | planning: incident -> last planning record | 8 | 26.6 | 57.5 | 23.0 | 57.5 | run folder |
| agent | model calls: sum per run (part of planning) | 16 | 44.6 | 66.6 | 21.9 | 109.3 | bundle |
| agent | Guard: planning end -> ALLOW (incl. lab approval) | 8 | 0.4 | 0.4 | 0.4 | 0.4 | run folder |
| agent | incident -> Guard ALLOW (planning + Guard) | 16 | 46.7 | 111.8 | 23.3 | 355.8 | bundle |
| agent | execution: ALLOW -> Executor result | 16 | 0.7 | 0.8 | 0.6 | 0.8 | bundle |
| agent | verification: Executor result -> outcome window end | 16 | 43.6 | 55.2 | 8.0 | 61.5 | bundle |
| agent | fault to repair: start -> Executor result | 16 | 54.6 | 119.6 | 31.5 | 363.7 | bundle; start: first failing snapshot x13, injection x3 |
| agent | total: start -> outcome window end | 16 | 100.8 | 148.5 | 58.0 | 407.0 | bundle; start: first failing snapshot x13, injection x3 |
| harness | detection: start -> incident observed | 5 | 7.2 | 7.4 | 7.1 | 7.4 | bundle; start: first failing snapshot x4, injection x1 |
| harness | planning: incident -> last planning record | 1 | 2.8 | 2.8 | 2.8 | 2.8 | bundle |
| harness | model calls: sum per run (part of planning) | 0 | n/a | n/a | n/a | n/a | - |
| harness | Guard: planning end -> ALLOW (incl. lab approval) | 1 | 0.3 | 0.3 | 0.3 | 0.3 | bundle |
| harness | incident -> Guard ALLOW (planning + Guard) | 5 | 2.8 | 3.1 | 2.8 | 3.1 | bundle |
| harness | execution: ALLOW -> Executor result | 5 | 0.6 | 0.7 | 0.6 | 0.7 | bundle |
| harness | verification: Executor result -> outcome window end | 5 | 26.6 | 55.5 | 26.1 | 55.5 | bundle |
| harness | fault to repair: start -> Executor result | 5 | 10.7 | 11.2 | 10.5 | 11.2 | bundle; start: first failing snapshot x4, injection x1 |
| harness | total: start -> outcome window end | 5 | 37.2 | 66.7 | 36.7 | 66.7 | bundle; start: first failing snapshot x4, injection x1 |
<!-- END GENERATED:latency -->

### Model calls and cold loads

A call is cold when Ollama reports a model load over 1 s. Agent runs before
16:00 UTC on 11 November did not record load times, so their calls are
`unclassified`, not warm. The cold-load tail is the max column here and in
the stage table. The 356 s decision stage of run
`boutique-live-20261110T192806Z-88c2d9` holds only 53 s of recorded model
calls; its files do not show where the rest went, so it is not counted as a
cold load. The three qwen3:14b runs at 4afc879 used the bounded model
transport after a warm-up; their `model-runtime.json` records Q4_K_M fully on
the GPU with `keep_alive` 30m, and all nine of their calls were warm. No cold
load during a run has been measured with that transport.

<!-- BEGIN GENERATED:model-calls -->
| Mode | Measure | n | Median s | p90 s | Max s |
| --- | --- | --- | --- | --- | --- |
| agent | model time per run, warm runs | 7 | 25.1 | 56.1 | 56.1 |
| agent | model time per run, cold runs | 1 | 43.1 | 43.1 | 43.1 |
| agent | model time per run, unclassified runs | 8 | 50.3 | 109.3 | 109.3 |
| agent | one call, warm | 24 | 8.4 | 19.3 | 22.0 |
| agent | one call, cold | 2 | 32.5 | 41.1 | 41.1 |
| agent | one call, unclassified | 33 | 11.2 | 28.9 | 46.3 |
| agent | model load time of cold calls | 2 | 21.1 | 25.6 | 25.6 |
<!-- END GENERATED:model-calls -->

## Chaos Mesh faults

Injection and cleanup only: Sentinel takes no recovery action in these runs.
Every planned fault counts as an attempt, including batches that failed early.

<!-- BEGIN GENERATED:chaos -->
| Fault | Attempts | Injected | Cleaned up | Healthy recovery window |
| --- | --- | --- | --- | --- |
| cpu | 6 | 3/6 | 4/6 | 2/6 |
| dns | 5 | 0/5 | 2/5 | 0/5 |
| http | 3 | 0/3 | 0/3 | 0/3 |
| memory | 3 | 1/3 | 1/3 | 1/3 |
| network | 6 | 1/6 | 1/6 | 1/6 |
| pod | 6 | 4/6 | 4/6 | 1/6 |
<!-- END GENERATED:chaos -->

## Security lab

Blocked probes are the bypass, identity and outage probes that must fail.

<!-- BEGIN GENERATED:security-lab -->
| Bundle | Outcome | Valid mTLS quote | Blocked probes | Shopping after restore |
| --- | --- | --- | --- | --- |
| protected-boutique | PASS | yes | 8/8 | yes |
| protected-boutique-context-231935 | PASS | yes | 8/8 | yes |
| protected-boutique-pr5-230557 | PASS | yes | 8/8 | yes |
<!-- END GENERATED:security-lab -->

## Observer acceptance

<!-- BEGIN GENERATED:observer -->
| Bundle | Outcome | Detection s | Duplicate suppression s |
| --- | --- | --- | --- |
| boutique-observer-20261110t191257z-f68928 | PASS | 7.2 | 61.0 |
| boutique-observer-20261110t211902z-11fb75 | PASS | 7.2 | 60.2 |
| boutique-observer-20261111t143734z-2fd7ce | PASS | 7.4 | 61.9 |
<!-- END GENERATED:observer -->

## Claims

From [evidence/claims.json](../evidence/claims.json). Quote a claim with its
scope caveat.

<!-- BEGIN GENERATED:claims -->
| Claim | Statement | Scope caveat | Bundles |
| --- | --- | --- | --- |
| C-CONNECTED-HARNESS | The connected Boutique walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) passed in 4/4 runs | One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. Harness-authored proposals, no model. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 4 |
| C-AGENT-WARM | qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix | Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
| C-AGENT-ALL | Across every recorded agent-mode attempt, 14/24 reached RECOVERED | Includes deliberate negative tests (unreachable model server, missing model) and runs on older revisions. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred for the 20 runs before 4afc879 and recorded for the 4 runs at 4afc879. | 24 |
| C-AGENT-FAIL-CLOSED | Agent runs given an unreachable model server or a missing model stopped before any proposal reached Guard or the Executor in 6/6 runs | Deliberate negative tests. 'Stopped' is read from each bundle: the run failed with that error and wrote no proposal, Guard decision or execution record. The harness then removed the injected fault as an administrator emergency restore, which is not Sentinel recovery, and the incident stayed OPEN. One fault type (invalid shipping PORT). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 6 |
| C-GUARD-REJECTION | Guard rejection of an approved but unnecessary rollback: 2/2 refused, 0 Executor writes | One scenario: the service is healthy and already at its baseline. Counts Executor recovery writes only, after setup. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 2 |
| C-SECURITY-LAB | The protected Boutique security lab passed in 3/3 runs, blocking every bypass, identity and outage probe | Shipping path only, behind one mTLS gateway in a dedicated kind cluster; scripted probes, not an adversarial test. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
| C-CHAOS-BATCH | The latest Chaos Mesh demo batch (network, pod, cpu): injected 3/3, cleaned up 3/3 | Injection and cleanup only; Sentinel takes no recovery action. One batch in demo durations; earlier batches failed (see C-CHAOS-ALL). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 1 |
| C-CHAOS-ALL | Across every Chaos Mesh fault attempt: injected 9/29, cleaned up 12/29 | Includes development batches that failed before or during injection; the HTTP fault was never injected on this host. Injection only, no Sentinel action. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 17 |
| C-PACKAGED | The packaged runner image completed the Boutique walkthrough in 5/9 attempts | Attempts include image-build-only and interrupted starts. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 9 |
| C-OBSERVER | Live Observer acceptance on Boutique passed in 3/3 runs | Detects one Deployment failure mode (unavailable replicas after a bad rollout). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
| C-GUARD-EVAL | On the versioned synthetic Guard fixtures, unsafe proposals allowed per configuration: state_policy_only 21/27, full_available 0/27, no_validation 27/27 (definitional) | 33 hand-authored synthetic fixtures (27 unsafe, 6 safe) evaluated offline in one process; not live incidents and not an estimate of production frequency or generalisation. no_validation allows every case by construction, so its row is definitional, not measured. Admission dry-run and runtime preview are not included. Status PRELIMINARY_NOT_FROZEN: the issue #63 freeze conditions are not met. One Windows/amd64 host; provenance recorded. | 1 |
| C-CONNECTED-HARNESS-RECORDED | At a recorded revision, the connected Boutique walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) passed in 1/1 runs | One run. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. Harness-authored proposals, no model. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC. | 1 |
| C-GUARD-REJECTION-RECORDED | At a recorded revision, Guard rejection of an approved but unnecessary rollback: 1/1 refused, 0 Executor writes | One run of one scenario: the service is healthy and already at its baseline. The Executor made zero patch attempts for the refused action, counting recovery writes only, after setup. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC. | 1 |
| C-AGENT-RECORDED | At a recorded revision, qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs | Three runs only, on a warm model: each bundle's model-runtime.json records qwen3:14b Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call loaded in under 1 s. The preflight negative test that ran just before is counted in C-AGENT-PREFLIGHT-RECORDED, not here. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC. | 3 |
| C-AGENT-PREFLIGHT-RECORDED | At a recorded revision, agent mode given a model that is not installed failed at the model preflight, before any fault was injected and with no proposal, Guard decision or execution record, in 1/1 runs | One deliberate negative test (model qwen3:does-not-exist on the local Ollama server); the run stopped after 0.1 s and its summary records fault_injected false. It shows the fail-closed stop for a missing model only, not for a model that fails mid-run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4afc879 on integration/phase1, clean tree, 2026-11-11 UTC. | 1 |
| C-EXECUTOR-WEDGE | Live Executor wedge harness, main's Executor before the reconcile fix and integration/phase1 after it: rv-race at d5b2ed9 FAILED: NEEDS_RECONCILIATION on 5/5 reconcile passes, second proposal not admitted; rv-race at 4afc879 PASS: SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE on 5/5 reconcile passes, second proposal APPLIED; delete-recreate at 4afc879 PASS: SUPERSEDED/TARGET_REPLACED on 5/5 reconcile passes, second proposal APPLIED | One run per scenario, each in a disposable namespace on the local kind cluster; Guard is not under test. The before run copied the harness from 4afc879 into a checkout of main d5b2ed9 and replaced its shared.log import with a no-op stub. Covers a moved resourceVersion (rv-race) and a replaced target (delete-recreate) only: a rejected write that leaves the same UID at the same resourceVersion is not covered and stays NEEDS_RECONCILIATION until P2.06. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance inferred: the scenario summaries carry no provenance block, so each revision is the operator's statement of the checkout's HEAD at run time, recorded in the bundle MANIFEST basis. | 3 |
<!-- END GENERATED:claims -->
