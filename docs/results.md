# Results

Every table on this page is generated from the bundles in [evidence/](../evidence/README.md)
by `python scripts/evidence/summarize.py`. Do not edit the tables by hand: CI
runs `summarize.py --check` and fails on any difference. Prose outside the
tables is free text.

Read each number with its limits:

- Runs are on one Windows/amd64 host with Docker Desktop and kind. Each table
  counts runs, not independent trials; repeated runs share the host and model.
- Failed, interrupted and incomplete runs are counted in every denominator.
- `inferred` provenance means the run predates provenance stamping and its
  source revision is a best guess. Only `recorded` bundles prove a commit.
- The recovery scenario is one fault type (an invalid shipping PORT) where the
  correct action is always ROLLBACK, and lab approval is pre-authorised.

## Runs by class

<!-- BEGIN GENERATED:runs -->
| Class | Runs | PASS | FAILED | INTERRUPTED | INCOMPLETE | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| agent | 20 | 11 | 9 | 0 | 0 | 0 recorded, 20 inferred |
| chaos-batch | 17 | 3 | 13 | 0 | 1 | 0 recorded, 17 inferred |
| connected-harness | 4 | 4 | 0 | 0 | 0 | 0 recorded, 4 inferred |
| guard-rejection | 2 | 2 | 0 | 0 | 0 | 0 recorded, 2 inferred |
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
| connected-harness | healthy baseline captured with real service samples and immutable image | 4/4 |
| connected-harness | real ServiceAccount permits only selected Deployment get/patch; forbidden write denied | 4/4 |
| connected-harness | Connected cart and checkout fail while shipping configuration is broken | 4/4 |
| connected-harness | missing-approval makes zero executor writes | 4/4 |
| connected-harness | hard-policy-rejection makes zero executor writes | 4/4 |
| connected-harness | stale-state makes zero executor writes | 4/4 |
| connected-harness | real API rejects JSON Patch after resourceVersion changes | 4/4 |
| connected-harness | competing real worker is excluded before mutation | 4/4 |
| connected-harness | approved real rollback, duplicate replay and changed-ID rejection | 4/4 |
| connected-harness | Role 6 verified gRPC, readiness and template; Coordinator RECOVERED | 4/4 |
| connected-harness | gRPC quote and replica health after executor rollback; Role 6 published RECOVERED | 4/4 |
| connected-harness | actual worker process exits after live patch; restart reconciles without resubmission | 4/4 |
| connected-harness | Guard rejects unnecessary rollback despite approval; zero Executor patch attempts | 4/4 |
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
<!-- END GENERATED:agent-runs -->

## Guard rejection

A healthy service receives an approved but unnecessary rollback. Guard must
refuse it and the Executor must make no write.

<!-- BEGIN GENERATED:guard-rejection -->
| Runs | Refused | Executor writes for the refused action | Unmeasured |
| --- | --- | --- | --- |
| 2 | 2/2 | 0 | 0 |
<!-- END GENERATED:guard-rejection -->

## Timings

Successful runs only. `recorded` values come from the harness timeline;
`approx` values come from file timestamps in older bundles.

<!-- BEGIN GENERATED:timings -->
| Class | Interval | n | Median s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- |
| agent | incident observed -> repair applied | 11 | 48.3 | 27.1 | 356.5 | approx |
| agent | repair applied -> outcome window end | 11 | 43.8 | 28.9 | 61.5 | approx |
| connected-harness | incident observed -> repair applied | 4 | 3.4 | 3.4 | 3.8 | approx |
| connected-harness | repair applied -> outcome window end | 4 | 26.45 | 26.1 | 55.5 | approx |
<!-- END GENERATED:timings -->

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
| C-AGENT-ALL | Across every recorded agent-mode attempt, 11/20 reached RECOVERED | Includes deliberate negative tests (unreachable model server, missing model) and runs on older revisions. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 20 |
| C-GUARD-REJECTION | Guard rejection of an approved but unnecessary rollback: 2/2 refused, 0 Executor writes | One scenario: the service is healthy and already at its baseline. Counts Executor recovery writes only, after setup. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 2 |
| C-SECURITY-LAB | The protected Boutique security lab passed in 3/3 runs, blocking every bypass, identity and outage probe | Shipping path only, behind one mTLS gateway in a dedicated kind cluster; scripted probes, not an adversarial test. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
| C-CHAOS-BATCH | The latest Chaos Mesh demo batch (network, pod, cpu): injected 3/3, cleaned up 3/3 | Injection and cleanup only; Sentinel takes no recovery action. One batch in demo durations; earlier batches failed (see C-CHAOS-ALL). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 1 |
| C-CHAOS-ALL | Across every Chaos Mesh fault attempt: injected 9/29, cleaned up 12/29 | Includes development batches that failed before or during injection; the HTTP fault was never injected on this host. Injection only, no Sentinel action. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 17 |
| C-PACKAGED | The packaged runner image completed the Boutique walkthrough in 5/9 attempts | Attempts include image-build-only and interrupted starts. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 9 |
| C-OBSERVER | Live Observer acceptance on Boutique passed in 3/3 runs | Detects one Deployment failure mode (unavailable replicas after a bad rollout). One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
<!-- END GENERATED:claims -->
