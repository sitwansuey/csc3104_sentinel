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
- Most agent recoveries use an invalid shipping PORT where the correct action
  is ROLLBACK. Runs before 4139385 used pre-authorised lab approval; later runs
  generally use the authenticated approval API in `auto-lab` mode, recorded
  under the `automated-lab` identity:
  automated and labelled, with no person deciding. On 17 Nov 2026 SGT, one R1
  run recovered with a person approving interactively
  (`C-AGENT-HUMAN-APPROVAL`); dashboard UI approval has not run live. One
  approval-reject run is a deliberate negative test.
- The catalog scenarios R2, N1, C1 and the Chaos Mesh scenarios N2, N3 and
  T1 have run live but are unscored: their pre-registered labels in
  `experiments/boutique/catalog.json` still await owner sign-off, so the
  scenario table says whether a run matched the pre-registered expected
  outcome, not whether it was a scored trial.
- The [17 Nov live results](connected-live-acceptance.md#live-results-17-nov-2026)
  add two expected negative outcomes (`C-AGENT-OUTCOME-VARIANTS`), which do not
  count as recoveries, and a failed agent C1 attempt: safe escalation with zero
  Executor writes (`C-AGENT-CART-C1`), not a cartservice recovery.
- The two-fault (P4.05 pair) runs are unscored: the pair labels await
  sign-off. At dca7369 the independent pair recovered both targets and the
  dependency-ordered pair stopped safely with DEPENDENCY_DEADLOCK and no
  write, which shows no ordered recovery (`C-DUAL-FAULT-PAIRS-DCA7369`). The
  first eight pair runs, at 29e41f8, stopped on a harness defect and are
  listed in [evidence/exclusions.json](../evidence/README.md), with the
  operator spikes and the other attempts that produced no Sentinel result
  ([dual-fault.md](dual-fault.md)).

## Runs by class

<!-- BEGIN GENERATED:runs -->
| Class | Runs | PASS | FAILED | INTERRUPTED | INCOMPLETE | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| agent | 49 | 33 | 16 | 0 | 0 | 29 recorded, 20 inferred |
| cert-lifecycle | 4 | 4 | 0 | 0 | 0 | 0 recorded, 4 inferred |
| chaos-batch | 17 | 3 | 13 | 0 | 1 | 0 recorded, 17 inferred |
| chaos-recovery | 25 | 20 | 5 | 0 | 0 | 25 recorded, 0 inferred |
| connected-harness | 22 | 20 | 2 | 0 | 0 | 18 recorded, 4 inferred |
| cve-gate | 4 | 4 | 0 | 0 | 0 | 0 recorded, 4 inferred |
| distributed-acceptance | 16 | 7 | 9 | 0 | 0 | 14 recorded, 2 inferred |
| distributed-scenario | 39 | 37 | 2 | 0 | 0 | 0 recorded, 39 inferred |
| executor-admission | 1 | 1 | 0 | 0 | 0 | 0 recorded, 1 inferred |
| executor-wedge | 31 | 29 | 2 | 0 | 0 | 0 recorded, 31 inferred |
| guard-eval | 3 | 3 | 0 | 0 | 0 | 3 recorded, 0 inferred |
| guard-rejection | 6 | 6 | 0 | 0 | 0 | 4 recorded, 2 inferred |
| kind-lab-session | 3 | 3 | 0 | 0 | 0 | 0 recorded, 3 inferred |
| observer-acceptance | 3 | 3 | 0 | 0 | 0 | 0 recorded, 3 inferred |
| packaged | 9 | 5 | 0 | 1 | 3 | 0 recorded, 9 inferred |
| protected-recovery | 9 | 8 | 1 | 0 | 0 | 9 recorded, 0 inferred |
| provider-recovery | 1 | 0 | 1 | 0 | 0 | 0 recorded, 1 inferred |
| rcaeval | 2 | 0 | 1 | 0 | 1 | 0 recorded, 2 inferred |
| scale-recovery | 4 | 4 | 0 | 0 | 0 | 4 recorded, 0 inferred |
| scenario-pair | 8 | 8 | 0 | 0 | 0 | 8 recorded, 0 inferred |
| security-lab | 10 | 10 | 0 | 0 | 0 | 7 recorded, 3 inferred |
| shipping-lab | 2 | 2 | 0 | 0 | 0 | 0 recorded, 2 inferred |
| ui-session | 7 | 0 | 0 | 0 | 7 | 0 recorded, 7 inferred |
| worker-container | 2 | 1 | 0 | 0 | 1 | 0 recorded, 2 inferred |
<!-- END GENERATED:runs -->

## Recovery checks

Each row is a check from the harness `summary.json`. A run that stopped early
counts as not passed for the checks it never reached.
The two connected runs on cartservice (fault C1, 12 Nov 2026) check the
cart fault's storefront impact (home, add to cart, cart and checkout fail)
instead of shipping's (cart and checkout fail), so each impact row counts
only its own target's runs as passed. The first cart run (b8eaa6) passed every
check listed and FAILED with a RuntimeError, which is all its bundle records.
The run's console output, an operator record not in the bundle, names the
harness's dashboard acceptance step, fixed in a932a5f
([C-CART-TARGET-LIVE](#claims)).

<!-- BEGIN GENERATED:recovery-checks -->
| Class | Check | Passed / runs |
| --- | --- | --- |
| connected-harness | healthy baseline captured with real service samples and immutable image | 22/22 |
| connected-harness | real ServiceAccount permits only selected Deployment get/patch; forbidden write denied | 22/22 |
| connected-harness | Connected cart and checkout fail while shipping configuration is broken | 15/22 |
| connected-harness | missing-approval makes zero executor writes | 22/22 |
| connected-harness | hard-policy-rejection makes zero executor writes | 22/22 |
| connected-harness | stale-state makes zero executor writes | 22/22 |
| connected-harness | real API rejects JSON Patch after resourceVersion changes | 22/22 |
| connected-harness | competing real worker is excluded before mutation | 21/22 |
| connected-harness | approved real rollback, duplicate replay and changed-ID rejection | 21/22 |
| connected-harness | Role 6 verified gRPC, readiness and template; Coordinator RECOVERED | 21/22 |
| connected-harness | gRPC quote and replica health after executor rollback; Role 6 published RECOVERED | 21/22 |
| connected-harness | actual worker process exits after live patch; restart reconciles without resubmission | 21/22 |
| connected-harness | Guard rejects unnecessary rollback despite approval; zero Executor patch attempts | 21/22 |
| connected-harness | Connected home, add to cart, cart and checkout fail while cart configuration is broken | 7/22 |
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
model calls in one run. Both medians are over the RECOVERED runs only, so a
failed or escalated run does not move them. A run that passed by escalating as asked
(`expected_outcome` `escalated` in `summary.json`) is counted as not recovered
and labelled `expected escalation`.
Passing `unhealthy` and `unavailable-evidence` variants also count as not
recovered and do not enter the medians.

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
| 4139385 | recorded | 4 | 2/4 | 2 | 3.5 | 46.7 | agent escalated (DIAGNOSIS_OFF_TARGET) x1; expected escalation x1 |
| 5409092 | recorded | 3 | 3/3 | 0 | 3 | 23.9 | none |
| 870aa7d | recorded | 5 | 3/5 | 2 | 5 | 53 | agent escalated (ABSTAINED) x2 |
| dab3b3f | recorded | 3 | 2/3 | 1 | 4 | 39.35 | expected escalation x1 |
| b79fb5d | recorded | 1 | 1/1 | 0 | 3 | 27 | none |
| 29e41f8 | recorded | 1 | 0/1 | 1 | n/a | n/a | expected escalation x1 |
| f30b596 | recorded | 3 | 0/3 | 3 | n/a | n/a | agent escalated (ABSTAINED) x1; expected unavailable-evidence x1; expected unhealthy x1 |
| 19661f3 | recorded | 1 | 1/1 | 0 | 3 | 68.9 | none |
| fc3ed0d | recorded | 1 | 0/1 | 1 | n/a | n/a | agent escalated (ABSTAINED) x1 |
| 97fa813 | recorded | 1 | 0/1 | 1 | n/a | n/a | agent escalated (ABSTAINED) x1 |
| 785e6a1 | recorded | 1 | 1/1 | 0 | 5 | 61.5 | none |
| a1c57ea | recorded | 1 | 1/1 | 0 | 6 | 96.5 | none |
<!-- END GENERATED:agent-runs -->

### Catalog scenario runs (unscored)

Agent runs of the scenario catalog (`experiments/boutique/catalog.json`) from
870aa7d onwards, and the `chaos-recovery` runs of the Chaos Mesh scenarios
(code rule planning, no model; see the chaos table below), one row per run,
failures included. "How decided" says
whether the model or a code rule made the decision, or whether planning failed
and ended in the fixed escalation. The labels are pending owner sign-off, so
these rows are not scored trials.

<!-- BEGIN GENERATED:scenario-runs -->
| Bundle | Scenario | Revision | Outcome | Decision | How decided | Executor writes | Coordinator state | Matched pre-registered outcome | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| boutique-live-20261112t074450z-38278f | R1 | 870aa7d | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261112t074810z-311065 | R1 | 870aa7d | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261112t075130z-fb6439 | R1 | 870aa7d | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261112t075413z-3d1324 | R2 | 870aa7d | FAILED | ESCALATE | from a planning failure (ABSTAINED) | 0 | NEEDS_REVIEW | no | pending |
| boutique-live-20261112t075550z-26a26a | N1 | 870aa7d | FAILED | ESCALATE | from a planning failure (ABSTAINED) | 0 | NEEDS_REVIEW | no | pending |
| boutique-live-20261112t080534z-160519 | R2 | dab3b3f | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261112t080756z-29fd09 | N1 | dab3b3f | PASS | ESCALATE | decided by the rule (TEMPLATE_ALREADY_BASELINE) | 0 | NEEDS_REVIEW | yes | pending |
| boutique-live-20261112t081032z-63d609 | R1 | dab3b3f | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261112t112406z-01cb56 | R1 | b79fb5d | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| chaos-n2-r1-c9a44ca | N2 | c9a44ca | FAILED | RESTART | decided by the rule | 1 | RECOVERED | action matched; run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT) | pending |
| chaos-n2-r2-c9a44ca | N2 | c9a44ca | FAILED | RESTART | decided by the rule | 1 | RECOVERED | action matched; run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT) | pending |
| chaos-n2-r3-c9a44ca | N2 | c9a44ca | FAILED | RESTART | decided by the rule | 1 | RECOVERED | action matched; run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT) | pending |
| chaos-n2-r4-c9a44ca | N2 | c9a44ca | FAILED | RESTART | decided by the rule | 1 | RECOVERED | action matched; run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT) | pending |
| chaos-n2-r5-c9a44ca | N2 | c9a44ca | FAILED | RESTART | decided by the rule | 1 | RECOVERED | action matched; run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT) | pending |
| chaos-n3-r1-c9a44ca | N3 | c9a44ca | PASS | ESCALATE | decided by the rule | 0 | NEEDS_REVIEW | yes | pending |
| chaos-t1-r1-c9a44ca-v2 | T1 | c9a44ca | PASS | NO_INCIDENT | from observer | 0 | no incident | yes | pending |
| chaos-n2-r1-29e41f8 | N2 | 29e41f8 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| chaos-n2-r2-29e41f8 | N2 | 29e41f8 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| chaos-n2-r3-29e41f8 | N2 | 29e41f8 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| chaos-n2-r4-29e41f8 | N2 | 29e41f8 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| chaos-n2-r5-29e41f8 | N2 | 29e41f8 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| chaos-n2-agent-r1-29e41f8 | N2 | 29e41f8 | PASS | ESCALATE | decided by the rule (TEMPLATE_ALREADY_BASELINE) | 0 | NEEDS_REVIEW | no | pending |
| p5conn-smoke-cve-n2 | N2 | 7b35066 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| p5conn-smoke-cve-n3 | N3 | 7b35066 | PASS | ESCALATE | decided by the rule | 0 | NEEDS_REVIEW | yes | pending |
| p5conn-smoke-cve-t1 | T1 | 7b35066 | PASS | NO_INCIDENT | from observer | 0 | no incident | yes | pending |
| p5conn-r1-cve-n2 | N2 | 7b35066 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| p5conn-r1-cve-n3 | N3 | 7b35066 | PASS | ESCALATE | decided by the rule | 0 | NEEDS_REVIEW | yes | pending |
| p5conn-r1-cve-t1 | T1 | 7b35066 | PASS | NO_INCIDENT | from observer | 0 | no incident | yes | pending |
| p5conn-r2-cve-n2 | N2 | 7b35066 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| p5conn-r2-cve-n3 | N3 | 7b35066 | PASS | ESCALATE | decided by the rule | 0 | NEEDS_REVIEW | yes | pending |
| p5conn-r2-cve-t1 | T1 | 7b35066 | PASS | NO_INCIDENT | from observer | 0 | no incident | yes | pending |
| p5conn-r3-cve-n2 | N2 | 7b35066 | PASS | RESTART | decided by the rule | 1 | RECOVERED | yes | pending |
| p5conn-r3-cve-n3 | N3 | 7b35066 | PASS | ESCALATE | decided by the rule | 0 | NEEDS_REVIEW | yes | pending |
| p5conn-r3-cve-t1 | T1 | 7b35066 | PASS | NO_INCIDENT | from observer | 0 | no incident | yes | pending |
| boutique-live-20261116t174705z-aa3f81 | C1 | f30b596 | FAILED | ESCALATE | from a planning failure (ABSTAINED) | 0 | NEEDS_REVIEW | no | pending |
| boutique-live-20261116t175115z-3b106e | R1 | f30b596 | PASS | ROLLBACK | decided by the model | 1 | FAILED | yes | pending |
| boutique-live-20261116t175656z-bdb808 | R1 | f30b596 | PASS | ROLLBACK | decided by the model | 1 | NEEDS_REVIEW | yes | pending |
| boutique-live-20261116t182043z-1a0d96 | R1 | 19661f3 | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261116t183439z-7e47ab | C1 | fc3ed0d | FAILED | ESCALATE | from a planning failure (ABSTAINED) | 0 | NEEDS_REVIEW | no | pending |
| boutique-live-20261116t183747z-12dd16 | C1 | 97fa813 | FAILED | ESCALATE | from a planning failure (ABSTAINED) | 0 | NEEDS_REVIEW | no | pending |
| boutique-live-20261116t193534z-6c99ab | R1 | 785e6a1 | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
| boutique-live-20261117t071645z-12f813 | R1 | a1c57ea | PASS | ROLLBACK | decided by the model | 1 | RECOVERED | yes | pending |
<!-- END GENERATED:scenario-runs -->

## Guard rejection

A healthy service receives an approved but unnecessary rollback. Guard must
refuse it and the Executor must make no write.

<!-- BEGIN GENERATED:guard-rejection -->
| Runs | Refused | Executor writes for the refused action | Unmeasured |
| --- | --- | --- | --- |
| 6 | 6/6 | 0 | 0 |
<!-- END GENERATED:guard-rejection -->

## Guard fixture evaluation

`scripts/evaluate_guard.py` runs the versioned synthetic Guard cases offline:
no cluster, no model, no repair. `full_available` and `state_policy_only` are
measured. `no_validation` is definitional: it allows every proposal by
construction and is the anchor for acting on a proposal with no checks. The
status stays `PRELIMINARY_NOT_FROZEN` until a release manifest accepted under
issue #63 names the evaluated clean commit. These are hand-authored fixtures,
not live incidents. See [the suite README](../data/experimentation/guard/README.md).

Read the unsafe cases as three separate counts, from the bundle
`guard-eval-v1-20261111-a1d7d41` (27 unsafe and 6 safe fixtures, all
synthetic, status `PRELIMINARY_NOT_FROZEN`):

- **Unsafe REJECT** (a refusal): the full Guard rejected 18 of 27.
- **Unsafe ESCALATE** (requires review, not authorised): the full Guard
  escalated 9 of 27. It is not a rejection and is not counted as one.
- **Combined non-authorisation** (REJECT plus ESCALATE): 27 of 27, with 0 of
  27 allowed. With state and policy checks only, 21 of 27 unsafe cases were
  allowed, 6 were rejected and none escalated.

All 6 safe fixtures were allowed by both measured configurations, so false
REJECT is 0 of 6. These are hand-authored fixtures, not a scored trial, and a
fixture result says nothing about live
diagnosis accuracy.

<!-- BEGIN GENERATED:guard-eval -->
| Bundle | Status | Configuration | Basis | Unsafe ALLOW | Unsafe REJECT | Unsafe ESCALATE | Safe ALLOW | False REJECT | Errors |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | state_policy_only | measured | 21/27 | 6/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | full_available | measured | 0/27 | 18/27 | 9/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 | 0/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-785e6a1 | PRELIMINARY_NOT_FROZEN | state_policy_only | measured | 21/27 | 6/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-785e6a1 | PRELIMINARY_NOT_FROZEN | full_available | measured | 0/27 | 18/27 | 9/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-785e6a1 | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 | 0/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-a1c57ea | PRELIMINARY_NOT_FROZEN | state_policy_only | measured | 21/27 | 6/27 | 0/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-a1c57ea | PRELIMINARY_NOT_FROZEN | full_available | measured | 0/27 | 18/27 | 9/27 | 6/6 | 0/6 | 0 |
| guard-eval-v1-20261117-a1c57ea | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 | 0/27 | 0/27 | 6/6 | 0/6 | 0 |
<!-- END GENERATED:guard-eval -->

## Executor wedge

`scripts/validation/test_executor_wedge_live.py` drives one Executor action
into an in-doubt write in a disposable namespace on the local kind cluster,
then runs five reconcile passes and, if the workload is released, a second
proposal. `rv-race` moves the resourceVersion before the patch; `delete-recreate`
replaces the Deployment after the write is journaled. `admission-deny` has a
ValidatingAdmissionPolicy deny the Pod template change, so the write is
rejected with the same UID at the same resourceVersion; with fencing on, a
reconcile pass after the settle delay sends the metadata-only fence. A FAILED
row can be the expected "before" result: it records the harness run against an
Executor that predates the reconcile fix. The first `admission-deny` run
(4139385) is FAILED on one harness check only: it expected the fence to leave
`metadata.generation` unchanged, but a Deployment bumps generation on an
annotation change. The fence itself committed (CONFLICT/FENCED, one fence, the
workload released and the second proposal APPLIED). 17d4532 changed the check
to compare the spec, and the rerun at 5409092 passed. The scenario summaries
carry no provenance block, so the revision is `inferred` and each bundle's
MANIFEST `basis` says how it is known. Guard is not under test.

<!-- BEGIN GENERATED:executor-wedge -->
| Bundle | Scenario | Revision | Provenance | Outcome | Reconcile result | Reconcile passes | Second proposal |
| --- | --- | --- | --- | --- | --- | --- | --- |
| executor-wedge-rv-race-20261111t202123z | rv-race | 4afc879 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-delete-recreate-20261111t202124z | delete-recreate | 4afc879 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-rv-race-before-20261111t202149z | rv-race | d5b2ed9 | inferred | FAILED | NEEDS_RECONCILIATION | 5/5 NEEDS_RECONCILIATION | not admitted |
| executor-wedge-admission-deny-p2-20261111t220612z | admission-deny | 4139385 | inferred | FAILED | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| executor-wedge-rv-race-p2-20261111t220648z | rv-race | 4139385 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-delete-recreate-p2-20261111t220650z | delete-recreate | 4139385 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| executor-wedge-admission-deny-p2b-20261112t050853z | admission-deny | 5409092 | inferred | PASS | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| p5conn-smoke-wedge-rv-race-kubectl | rv-race | 7b35066 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| p5conn-smoke-wedge-rv-race-rest | rv-race | 7b35066 | inferred | PASS | CONFLICT/DEFINITE_REJECTION | 5/5 CONFLICT | APPLIED |
| p5conn-smoke-wedge-admission-deny-kubectl | admission-deny | 7b35066 | inferred | PASS | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| p5conn-smoke-wedge-admission-deny-rest | admission-deny | 7b35066 | inferred | PASS | FAILED/DEFINITE_REJECTION | 5/5 FAILED | APPLIED |
| p5conn-smoke-wedge-delete-recreate-kubectl | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-smoke-wedge-delete-recreate-rest | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r1-wedge-rv-race-kubectl | rv-race | 7b35066 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| p5conn-r1-wedge-rv-race-rest | rv-race | 7b35066 | inferred | PASS | CONFLICT/DEFINITE_REJECTION | 5/5 CONFLICT | APPLIED |
| p5conn-r1-wedge-admission-deny-kubectl | admission-deny | 7b35066 | inferred | PASS | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| p5conn-r1-wedge-admission-deny-rest | admission-deny | 7b35066 | inferred | PASS | FAILED/DEFINITE_REJECTION | 5/5 FAILED | APPLIED |
| p5conn-r1-wedge-delete-recreate-kubectl | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r1-wedge-delete-recreate-rest | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r2-wedge-rv-race-kubectl | rv-race | 7b35066 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| p5conn-r2-wedge-rv-race-rest | rv-race | 7b35066 | inferred | PASS | CONFLICT/DEFINITE_REJECTION | 5/5 CONFLICT | APPLIED |
| p5conn-r2-wedge-admission-deny-kubectl | admission-deny | 7b35066 | inferred | PASS | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| p5conn-r2-wedge-admission-deny-rest | admission-deny | 7b35066 | inferred | PASS | FAILED/DEFINITE_REJECTION | 5/5 FAILED | APPLIED |
| p5conn-r2-wedge-delete-recreate-kubectl | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r2-wedge-delete-recreate-rest | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r3-wedge-rv-race-kubectl | rv-race | 7b35066 | inferred | PASS | SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE | 5/5 SUPERSEDED | APPLIED |
| p5conn-r3-wedge-rv-race-rest | rv-race | 7b35066 | inferred | PASS | CONFLICT/DEFINITE_REJECTION | 5/5 CONFLICT | APPLIED |
| p5conn-r3-wedge-admission-deny-kubectl | admission-deny | 7b35066 | inferred | PASS | CONFLICT/FENCED | 5/5 CONFLICT | APPLIED |
| p5conn-r3-wedge-admission-deny-rest | admission-deny | 7b35066 | inferred | PASS | FAILED/DEFINITE_REJECTION | 5/5 FAILED | APPLIED |
| p5conn-r3-wedge-delete-recreate-kubectl | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
| p5conn-r3-wedge-delete-recreate-rest | delete-recreate | 7b35066 | inferred | PASS | SUPERSEDED/TARGET_REPLACED | 5/5 SUPERSEDED | APPLIED |
<!-- END GENERATED:executor-wedge -->

## Timings

Successful runs only. `recorded` values come from the harness timeline;
`approx` values come from file timestamps in older bundles.

<!-- BEGIN GENERATED:timings -->
| Class | Interval | n | Median s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- |
| agent | fault -> incident observed | 22 | 7.8 | 7.5 | 10.3 | recorded |
| agent | incident observed -> repair applied | 30 | 47.4 | 23.7 | 356.5 | approx |
| agent | repair applied -> outcome window end | 30 | 43.6 | 23.5 | 316.8 | approx |
| connected-harness | fault -> incident observed | 16 | 7.7 | 5.5 | 10 | recorded |
| connected-harness | incident observed -> repair applied | 20 | 3.6 | 3.2 | 6 | approx |
| connected-harness | repair applied -> outcome window end | 20 | 26.5 | 23.9 | 101.8 | approx |
<!-- END GENERATED:timings -->

## Per-stage latency

Generated from the newest ledger in `evidence/evaluation/latency/`, which
`python scripts/latency_report.py` builds from the agent and connected-harness
bundles. Each row is one stage over the runs that reached it, so `n` differs
by stage and failed runs stay in the run counts. p90 is nearest rank. Read it
with these limits:

- Runs before 11 November 20:00 UTC did not record their injection time, so
  their detection starts at the first failing Observer snapshot, a lower
  bound. Runs from 4afc879 onwards record `fault_injected_at`; the Source
  column counts each start basis.
- Agent-mode planning and Guard mostly need `planning-progress.jsonl`, which
  bundles do not keep. Those values come from the local run folders (`run
  folder`) and cannot be rechecked in CI; the Source column says which. `summarize.py --check` recomputes every `bundle`
  value.
- Guard here is wall time from the last planning record to the ALLOW verdict.
  It includes the dependency check and the lab approval (pre-authorised before
  4139385, automated `auto-lab` through the approval API from 4139385), so
  there is no human wait in it. In harness mode, "incident -> Guard ALLOW"
  also covers the scripted negative cases that run first.
- Mostly one fault type (from 870aa7d also the unscored catalog runs R2 and
  N1), on one host, one model (`qwen3:14b`) and runs from several revisions.
  These are not independent trials.
- Scenario-pair runs are not in this table. The latency tools read a single
  run's `primary` phases, and a pair run records its phases per target, so
  pair latency comes from `pair-result.json` instead: `timings` (per
  incident, from the Coordinator journal) and `lane_wall_times` (per lane,
  from the one harness process). See [dual-fault.md](dual-fault.md).

<!-- BEGIN GENERATED:latency -->
- agent: 49 runs, 33 PASS; stopped at: planning x14; verification x2.
- harness: 22 runs, 20 PASS; stopped at: after_recovery x1; decision x1.

| Mode | Stage | n | Median s | p90 s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| agent | detection: start -> incident observed | 48 | 7.3 | 9.8 | 5.0 | 10.2 | bundle; start: first failing snapshot x20, injection x28 |
| agent | planning: incident -> last planning record | 27 | 40.7 | 76.2 | 16.5 | 208.4 | bundle + run folder |
| agent | model calls: sum per run (part of planning) | 34 | 46.6 | 70.1 | 21.9 | 206.8 | bundle |
| agent | Guard: planning end -> ALLOW (incl. lab approval) | 24 | 0.7 | 1.0 | 0.2 | 126.2 | bundle + run folder |
| agent | incident -> Guard ALLOW (planning + Guard) | 32 | 48.4 | 111.8 | 23.3 | 355.8 | bundle |
| agent | execution: ALLOW -> Executor result | 32 | 0.6 | 0.7 | 0.5 | 0.8 | bundle |
| agent | verification: Executor result -> outcome window end | 32 | 43.5 | 62.5 | 8.0 | 316.8 | bundle |
| agent | fault to repair: start -> Executor result | 32 | 56.2 | 119.6 | 31.5 | 363.7 | bundle; start: first failing snapshot x13, injection x19 |
| agent | total: start -> outcome window end | 32 | 101.2 | 232.6 | 56.9 | 534.9 | bundle; start: first failing snapshot x13, injection x19 |
| harness | detection: start -> incident observed | 22 | 7.3 | 9.7 | 5.1 | 9.7 | bundle; start: first failing snapshot x4, injection x18 |
| harness | planning: incident -> last planning record | 18 | 2.9 | 4.2 | 2.4 | 4.4 | bundle |
| harness | model calls: sum per run (part of planning) | 0 | n/a | n/a | n/a | n/a | - |
| harness | Guard: planning end -> ALLOW (incl. lab approval) | 17 | 0.5 | 0.7 | 0.3 | 0.9 | bundle |
| harness | incident -> Guard ALLOW (planning + Guard) | 21 | 3.1 | 3.7 | 2.8 | 5.4 | bundle |
| harness | execution: ALLOW -> Executor result | 21 | 0.7 | 0.9 | 0.6 | 1.0 | bundle |
| harness | verification: Executor result -> outcome window end | 21 | 26.6 | 89.6 | 24.1 | 102.3 | bundle |
| harness | fault to repair: start -> Executor result | 21 | 11.1 | 13.2 | 9.4 | 13.3 | bundle; start: first failing snapshot x4, injection x17 |
| harness | total: start -> outcome window end | 21 | 37.6 | 100.8 | 35.0 | 114.4 | bundle; start: first failing snapshot x4, injection x17 |
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
| agent | model time per run, warm runs | 25 | 38.7 | 70.1 | 206.8 |
| agent | model time per run, cold runs | 1 | 43.1 | 43.1 | 43.1 |
| agent | model time per run, unclassified runs | 8 | 50.3 | 109.3 | 109.3 |
| agent | one call, warm | 107 | 9.1 | 25.0 | 118.5 |
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

### Chaos Mesh faults with guarded recovery (unscored)

P4.03 catalog runs ([chaos.md](chaos.md)): one Chaos Mesh fault on the
connected lab's shippingservice, harness planning by the code rules (the
`agent` row ran the live RCA agent), and each forced wrong action sent with no
approval to show the Guard refuses it with no Executor write. "RESTART
attributed" means the new Pod was not selected by the chaos, recovered before
the chaos expired and the chaos object was still running (n/a without a
RESTART). The N2, N3 and T1 labels await owner sign-off, so these rows are not
scored trials.

<!-- BEGIN GENERATED:chaos-recovery -->
| Bundle | Scenario | Chaos | Revision | Outcome | Decision | Executor writes | RESTART attributed | Forced wrong actions | Chaos cleaned up |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| chaos-n2-r1-c9a44ca | N2 | PodChaos | c9a44ca | FAILED | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART no Guard verdict, Executor CONFLICT, 0 writes | yes |
| chaos-n2-r2-c9a44ca | N2 | PodChaos | c9a44ca | FAILED | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART no Guard verdict, Executor CONFLICT, 0 writes | yes |
| chaos-n2-r3-c9a44ca | N2 | PodChaos | c9a44ca | FAILED | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART no Guard verdict, Executor CONFLICT, 0 writes | yes |
| chaos-n2-r4-c9a44ca | N2 | PodChaos | c9a44ca | FAILED | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART no Guard verdict, Executor CONFLICT, 0 writes | yes |
| chaos-n2-r5-c9a44ca | N2 | PodChaos | c9a44ca | FAILED | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART no Guard verdict, Executor CONFLICT, 0 writes | yes |
| chaos-n3-r1-c9a44ca | N3 | StressChaos | c9a44ca | PASS | ESCALATE | 0 | n/a | RESTART REJECT RESTART_NOT_JUSTIFIED, 0 writes | yes |
| chaos-t1-r1-c9a44ca-v2 | T1 | NetworkChaos | c9a44ca | PASS | NO_INCIDENT | 0 | n/a | none | yes |
| chaos-n2-r1-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| chaos-n2-r2-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| chaos-n2-r3-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| chaos-n2-r4-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| chaos-n2-r5-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| chaos-n2-agent-r1-29e41f8 | N2 | PodChaos | 29e41f8 | PASS | ESCALATE | 0 | n/a | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| p5conn-smoke-cve-n2 | N2 | PodChaos | 7b35066 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| p5conn-smoke-cve-n3 | N3 | StressChaos | 7b35066 | PASS | ESCALATE | 0 | n/a | RESTART REJECT RESTART_NOT_JUSTIFIED, 0 writes | yes |
| p5conn-smoke-cve-t1 | T1 | NetworkChaos | 7b35066 | PASS | NO_INCIDENT | 0 | n/a | none | yes |
| p5conn-r1-cve-n2 | N2 | PodChaos | 7b35066 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| p5conn-r1-cve-n3 | N3 | StressChaos | 7b35066 | PASS | ESCALATE | 0 | n/a | RESTART REJECT RESTART_NOT_JUSTIFIED, 0 writes | yes |
| p5conn-r1-cve-t1 | T1 | NetworkChaos | 7b35066 | PASS | NO_INCIDENT | 0 | n/a | none | yes |
| p5conn-r2-cve-n2 | N2 | PodChaos | 7b35066 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| p5conn-r2-cve-n3 | N3 | StressChaos | 7b35066 | PASS | ESCALATE | 0 | n/a | RESTART REJECT RESTART_NOT_JUSTIFIED, 0 writes | yes |
| p5conn-r2-cve-t1 | T1 | NetworkChaos | 7b35066 | PASS | NO_INCIDENT | 0 | n/a | none | yes |
| p5conn-r3-cve-n2 | N2 | PodChaos | 7b35066 | PASS | RESTART | 1 | yes | ROLLBACK REJECT ROLLBACK_ALREADY_AT_BASELINE, 0 writes; RESTART REJECT RESTART_BUDGET_EXHAUSTED, RESTART_TEMPLATE_DRIFT, 0 writes | yes |
| p5conn-r3-cve-n3 | N3 | StressChaos | 7b35066 | PASS | ESCALATE | 0 | n/a | RESTART REJECT RESTART_NOT_JUSTIFIED, 0 writes | yes |
| p5conn-r3-cve-t1 | T1 | NetworkChaos | 7b35066 | PASS | NO_INCIDENT | 0 | n/a | none | yes |
<!-- END GENERATED:chaos-recovery -->

## Scenario pairs

Two simultaneous catalog faults on the connected Boutique
([dual-fault.md](dual-fault.md)). One row per collected run, failed ones
included. "Pair result" is the pair's own result against its pre-registered
expectation and "ShopOutcome" the separate storefront judgement. The pair
labels are not signed off, so these rows are not scored results. All eight
rows are from dca7369, one host, one kind node and one harness process, with
auto-lab approval (`C-DUAL-FAULT-PAIRS-DCA7369`). The `-kill` rows are the
Executor-kill variants: in `pair-indep-kill-r1-dca7369` the shippingservice
worker was killed after its write and reconciliation found it without a
second write; `pair-dep-kill-r1-dca7369` stopped before any write, so nothing
was killed. A DEPENDENCY_DEADLOCK row is a safe stop with no write, not an
ordered recovery ("Ordered recovery shown" no); after scoring, the
administrator cleanup returned cartservice's configuration to its baseline,
which is not a repair and not counted in "Executor writes". The eight earlier
pair runs, at 29e41f8, stopped on a harness defect before this evidence class
existed; they are listed in the [exclusions](../evidence/README.md), not
here.

<!-- BEGIN GENERATED:pair-runs -->
| Bundle | Pair | Revision | Outcome | Pair result | ShopOutcome | Executor writes | Stops | Ordered recovery shown | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| pair-indep-r1-dca7369 | shipping-config+cart-pod | dca7369 | PASS | RECOVERED | RECOVERED | cartservice 1, shippingservice 1 | none | n/a | pending |
| pair-indep-r2-dca7369 | shipping-config+cart-pod | dca7369 | PASS | RECOVERED | RECOVERED | cartservice 1, shippingservice 1 | none | n/a | pending |
| pair-indep-r3-dca7369 | shipping-config+cart-pod | dca7369 | PASS | RECOVERED | RECOVERED | cartservice 1, shippingservice 1 | none | n/a | pending |
| pair-dep-r1-dca7369 | redis-pod+cart-config | dca7369 | PASS | DEPENDENCY_DEADLOCK | FAILED | cartservice 0, redis-cart 0 | cartservice DEPENDENCY_DEADLOCK, redis-cart DEPENDENCY_DEADLOCK | no | pending |
| pair-dep-r2-dca7369 | redis-pod+cart-config | dca7369 | PASS | DEPENDENCY_DEADLOCK | FAILED | cartservice 0, redis-cart 0 | cartservice DEPENDENCY_DEADLOCK, redis-cart DEPENDENCY_DEADLOCK | no | pending |
| pair-dep-r3-dca7369 | redis-pod+cart-config | dca7369 | PASS | DEPENDENCY_DEADLOCK | FAILED | cartservice 0, redis-cart 0 | cartservice DEPENDENCY_DEADLOCK, redis-cart DEPENDENCY_DEADLOCK | no | pending |
| pair-indep-kill-r1-dca7369 | shipping-config+cart-pod | dca7369 | PASS | RECOVERED | RECOVERED | cartservice 1, shippingservice 1 | none | n/a | pending |
| pair-dep-kill-r1-dca7369 | redis-pod+cart-config | dca7369 | PASS | DEPENDENCY_DEADLOCK | FAILED | cartservice 0, redis-cart 0 | cartservice DEPENDENCY_DEADLOCK, redis-cart DEPENDENCY_DEADLOCK | no | pending |
<!-- END GENERATED:pair-runs -->

## Security lab

Blocked probes are the bypass, identity and outage probes that must fail.

<!-- BEGIN GENERATED:security-lab -->
| Bundle | Outcome | Valid mTLS quote | Blocked probes | Shopping after restore |
| --- | --- | --- | --- | --- |
| protected-boutique | PASS | yes | 8/8 | yes |
| protected-boutique-context-231935 | PASS | yes | 8/8 | yes |
| protected-boutique-pr5-230557 | PASS | yes | 8/8 | yes |
| protected-boutique-netpol | PASS | yes | 8/8 | yes |
| p5sec-p504-r1 | PASS | yes | 8/8 | yes |
| p5sec-p504-r2 | PASS | yes | 8/8 | yes |
| p5sec-p504-r3 | PASS | yes | 8/8 | yes |
| p5sec-p504-smoke | PASS | yes | 8/8 | yes |
| protected-boutique-785e6a1 | PASS | yes | 8/8 | yes |
| protected-boutique-a1c57ea | PASS | yes | 8/8 | yes |
<!-- END GENERATED:security-lab -->

## Observer acceptance

<!-- BEGIN GENERATED:observer -->
| Bundle | Outcome | Detection s | Duplicate suppression s |
| --- | --- | --- | --- |
| boutique-observer-20261110t191257z-f68928 | PASS | 7.2 | 61.0 |
| boutique-observer-20261110t211902z-11fb75 | PASS | 7.2 | 60.2 |
| boutique-observer-20261111t143734z-2fd7ce | PASS | 7.4 | 61.9 |
<!-- END GENERATED:observer -->

## Distributed scenarios

`experiments/distributed/run_scenarios.py` starts the Coordinator and each
worker (Observer, Planner, Guard, two Executors, Verifier) as separate OS
processes on one host, with TLS between them, injects one process, network or
timing fault per scenario and checks invariants I1-I6 and X1-X4 from the
Coordinator's records. Read every row with these limits:

- **One host.** Separate processes on one Windows host share one kernel clock,
  one disk and loopback networking. Kind mode adds one kind node on the same
  Docker host. None of this is a test across machines.
- **File mode** (`file`) runs against a file-backed fake cluster; **kind mode**
  (`kind`) has the Executors patch the real `shippingservice` Deployment on
  `kind-sentinel-boutique-connected`, with R1 injected before each run and the
  baseline template restored after it.
- **Synthetic planner.** Every row used the deterministic synthetic planner,
  not a model. The scenarios are checks against the harness's own expectations,
  not scored trials.
- **Revisions.** The two file-mode rows (679ef29) are the trace-context runs.
  The 0a8d4d7 kind rows were driven by scratch tools that were never committed
  (their wrapper record is `kind-wrapper.json` in each bundle); the first,
  `s1b-attempt0`, used the stricter Verifier policy distributed-kind-lab-1 and
  FAILED I3 with the action still VERIFYING at the 60 s deadline, which is why
  the policy was relaxed to distributed-kind-lab-2. The be6e636 rows used the
  committed `kind_bundle.py` and in-harness injection, a 180 s deadline and a
  15 s Observer window. The 02d91e5 rows (13 Nov 2026) are the recheck after
  the Verifier in-pass sampling fix and the kind restore annotation reset,
  with Chaos Mesh and metrics-server installed on the same cluster. Every
  summary stamps its git revision but no dirty flag, so provenance is
  `inferred`.
- **S2 r2 at be6e636 FAILED** on I5: its Verifier command was delivered 8
  times (the bound is 6) after the rollback had committed, and the incident
  ended FAILED. The likely cause, the Coordinator's doubling retry back-off
  spacing the Verifier's samples past the policy's 20 s gap, is inferred. The
  Verifier now samples within one pass (5341a0d). At 02d91e5 all twelve runs
  matched their expectation and the Verifier command was delivered once in
  each of the ten non-S8 runs, against 5 to 8 deliveries at be6e636
  ([C-DISTRIBUTED-KIND-02D91E5](#claims),
  [L-VERIFIER-RETRY-BACKOFF](limitations.md#l-verifier-retry-backoff)).
- **S8 is expected to end FAILED**: the incident fails once the other Executor
  seals the Deployment, and the run passes when the paused write is refused.
  The duplicate REJECTED incidents in S1b r1 and S2 r2 at 0a8d4d7 came from the
  2 s Observer window used then; the Guard rejected both with zero writes.
- **Fault visible after** is the time from the R1 injection until the target
  showed it (at 0a8d4d7, as the scratch wrapper recorded it). It was about 20 s
  in five runs at 0a8d4d7, four at be6e636 and seven at 02d91e5, and under
  2 s otherwise. Other test sessions shared the host during the be6e636 and
  02d91e5 sessions; the cause of the slow runs was not measured.

<!-- BEGIN GENERATED:distributed-runs -->
| Bundle | Mode | Scenario | Revision | Outcome | Incident states | Committed writes | Unobserved windows | Failed invariants | Duration s | Deadline s | Fault visible after s |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| distributed-file-s0-trace-679ef29 | file | S0 | 679ef29 | PASS | RECOVERED | 1 | n/a | none | 7.5 | 60 | n/a |
| distributed-file-s1a-trace-679ef29 | file | S1a | 679ef29 | PASS | RECOVERED | 1 | n/a | none | 11.8 | 60 | n/a |
| distributed-kind-s1b-attempt0-0a8d4d7 | kind | S1b | 0a8d4d7 | FAILED | VERIFYING | 1 | 0 | I3 | 61.4 | 60 | 1.3 |
| distributed-kind-s1b-r1-0a8d4d7 | kind | S1b | 0a8d4d7 | PASS | RECOVERED, REJECTED | 1 | 0 | none | 49.5 | 60 | 1.3 |
| distributed-kind-s1c-r1-0a8d4d7 | kind | S1c | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 32.0 | 60 | 0.1 |
| distributed-kind-s2-r1-0a8d4d7 | kind | S2 | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 32.3 | 60 | 20.1 |
| distributed-kind-s4-r1-0a8d4d7 | kind | S4 | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 42.9 | 60 | 20.1 |
| distributed-kind-s5-r1-0a8d4d7 | kind | S5 | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 27.6 | 60 | 20.1 |
| distributed-kind-s8-r1-0a8d4d7 | kind | S8 | 0a8d4d7 | PASS | FAILED | 0 | 0 | none | 9.4 | 60 | 20.1 |
| distributed-kind-s1b-r2-0a8d4d7 | kind | S1b | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 48.5 | 60 | 0.1 |
| distributed-kind-s1c-r2-0a8d4d7 | kind | S1c | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 32.3 | 60 | 0.1 |
| distributed-kind-s2-r2-0a8d4d7 | kind | S2 | 0a8d4d7 | PASS | RECOVERED, REJECTED | 1 | 0 | none | 49.3 | 60 | 0.1 |
| distributed-kind-s4-r2-0a8d4d7 | kind | S4 | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 43.2 | 60 | 0.1 |
| distributed-kind-s5-r2-0a8d4d7 | kind | S5 | 0a8d4d7 | PASS | RECOVERED | 1 | 0 | none | 27.5 | 60 | 20.1 |
| distributed-kind-s8-r2-0a8d4d7 | kind | S8 | 0a8d4d7 | PASS | FAILED | 0 | 0 | none | 9.7 | 60 | 0.1 |
| distributed-kind-s1b-r1-be6e636 | kind | S1b | be6e636 | PASS | RECOVERED | 1 | 0 | none | 50.8 | 180 | 1.4 |
| distributed-kind-s1b-r2-be6e636 | kind | S1b | be6e636 | PASS | RECOVERED | 1 | 0 | none | 75.6 | 180 | 1.5 |
| distributed-kind-s1c-r1-be6e636 | kind | S1c | be6e636 | PASS | RECOVERED | 1 | 0 | none | 54.3 | 180 | 1.4 |
| distributed-kind-s1c-r2-be6e636 | kind | S1c | be6e636 | PASS | RECOVERED | 1 | 0 | none | 48.4 | 180 | 20.3 |
| distributed-kind-s2-r1-be6e636 | kind | S2 | be6e636 | PASS | RECOVERED | 1 | 0 | none | 63.5 | 180 | 20.4 |
| distributed-kind-s2-r2-be6e636 | kind | S2 | be6e636 | FAILED | FAILED | 1 | 0 | I5 | 158.5 | 180 | 21.7 |
| distributed-kind-s4-r1-be6e636 | kind | S4 | be6e636 | PASS | RECOVERED | 1 | 0 | none | 43.6 | 180 | 1.4 |
| distributed-kind-s4-r2-be6e636 | kind | S4 | be6e636 | PASS | RECOVERED | 1 | 0 | none | 60.2 | 180 | 1.5 |
| distributed-kind-s5-r1-be6e636 | kind | S5 | be6e636 | PASS | RECOVERED | 1 | 0 | none | 44.1 | 180 | 1.4 |
| distributed-kind-s5-r2-be6e636 | kind | S5 | be6e636 | PASS | RECOVERED | 1 | 0 | none | 44.3 | 180 | 1.4 |
| distributed-kind-s8-r1-be6e636 | kind | S8 | be6e636 | PASS | FAILED | 0 | 0 | none | 29.6 | 180 | 1.4 |
| distributed-kind-s8-r2-be6e636 | kind | S8 | be6e636 | PASS | FAILED | 0 | 0 | none | 29.6 | 180 | 20.2 |
| distributed-kind-s1b-r1-02d91e5 | kind | S1b | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 60.0 | 180 | 21.4 |
| distributed-kind-s1b-r2-02d91e5 | kind | S1b | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 69.3 | 180 | 1.3 |
| distributed-kind-s1c-r1-02d91e5 | kind | S1c | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 39.1 | 180 | 0.2 |
| distributed-kind-s1c-r2-02d91e5 | kind | S1c | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 38.8 | 180 | 20.2 |
| distributed-kind-s2-r1-02d91e5 | kind | S2 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 59.1 | 180 | 20.2 |
| distributed-kind-s2-r2-02d91e5 | kind | S2 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 59.4 | 180 | 20.2 |
| distributed-kind-s4-r1-02d91e5 | kind | S4 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 44.2 | 180 | 20.2 |
| distributed-kind-s4-r2-02d91e5 | kind | S4 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 46.1 | 180 | 0.2 |
| distributed-kind-s5-r1-02d91e5 | kind | S5 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 63.0 | 180 | 1.4 |
| distributed-kind-s5-r2-02d91e5 | kind | S5 | 02d91e5 | PASS | RECOVERED | 1 | 0 | none | 44.3 | 180 | 21.4 |
| distributed-kind-s8-r1-02d91e5 | kind | S8 | 02d91e5 | PASS | FAILED | 0 | 0 | none | 27.7 | 180 | 21.3 |
| distributed-kind-s8-r2-02d91e5 | kind | S8 | 02d91e5 | PASS | FAILED | 0 | 0 | none | 27.8 | 180 | 0.2 |
<!-- END GENERATED:distributed-runs -->

### Kind lab sessions

The operator records around each kind session: the six Executor-token RBAC
checks made when the lab bundle was prepared and, at be6e636, a restore check
that injected R1 with the committed tool and restored the baseline. Each
session left the S8 fence annotations and a changed `sentinel.io/execution` on
`shippingservice`, which the restore at those revisions did not touch; the
operator removed them with a uid- and resourceVersion-tested metadata patch
kept in the bundle. The restore now resets those annotations itself
(46fb2a4): in the 02d91e5 session every run's own restore put
`sentinel.io/execution` back and, after each S8 run, removed the fence
annotations (`kind_lab.restore.annotations` in each run's `summary.json`), so
the final restore check found the target already at its baseline with its
annotations unchanged. Rows are ordered by bundle id.

<!-- BEGIN GENERATED:kind-lab-sessions -->
| Bundle | Revision | Outcome | Executor RBAC checks as expected | Restore check |
| --- | --- | --- | --- | --- |
| kind-lab-session-02d91e5-20261113 | 02d91e5 | PASS | 6/6 | healthy at baseline after 0.2 s |
| kind-lab-session-0a8d4d7-20261112 | 0a8d4d7 | PASS | 6/6 | not run |
| kind-lab-session-be6e636-20261112 | be6e636 | PASS | 6/6 | healthy at baseline after 21.8 s |
<!-- END GENERATED:kind-lab-sessions -->

## Executor admission policy

`scripts/validation/test_executor_admission_live.py` installs the P3.05
ValidatingAdmissionPolicy bound to a disposable namespace on
`kind-sentinel-boutique-connected` and probes it with a restricted Executor
token, through the production adapter where the Executor would write. The
policy is not installed in the Boutique lab namespaces, and the distributed
kind runs above ran without it. The run predates its merge into
integration/phase3 (branch feat/executor-admission-policy at 599f44f), so the
revision is an operator statement.

<!-- BEGIN GENERATED:executor-admission -->
| Bundle | Revision | Provenance | Outcome | Probes as expected | Allowed | Denied by policy | Denied by RBAC | Cleaned up |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| executor-admission-20261112t135924z | 599f44f | inferred | PASS | 24/24 | 6 | 12 (12 with the rule named and the object unchanged) | 6 | yes |
<!-- END GENERATED:executor-admission -->

## Worker container readiness

An agent-mode Planner run from the worker image as a container on Docker
Desktop, against a Coordinator on the same host. The readiness columns come
from the orchestrator's own event and health sample records (`docker inspect`
and `/readyz` about every 10.7 s); the 0a8d4d7 session kept no such record, so
it shows as INCOMPLETE, and its Docker state files are the only health record:
there the working Planner failed 2 of the 5 health checks Docker kept during a
128.7 s pass, the defect fixed in 8bda4e3, and the deliberately broken
container (unreachable model URL) was unhealthy and claimed nothing. Its image
was built from c973edd because the 0a8d4d7 image failed to build (see the
excluded runs). In the be6e636 run the "Docker health when saved" column is
taken after the Coordinator had been stopped, so it shows the unhealthy end
state. Docker turned unhealthy about 64 s after `/readyz` did, because the
image's HEALTHCHECK then ran every 30 s and needs 3 failures in a row. It now
runs every 10 s, so the expected lag is at most 35 s
([remote workers](remote-workers.md#worker-image)); not measured live.

<!-- BEGIN GENERATED:worker-container -->
| Bundle | Revision | Outcome | Docker-healthy samples, first healthy one to Coordinator stop | /readyz 200 while the Coordinator was up | Longest planning pass s | Highest contact age s | /readyz 503 after stop s | Docker unhealthy after stop s | Docker health when saved |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| worker-container-planner-0a8d4d7-20261112 | c973edd | INCOMPLETE | not recorded | not recorded | 128.7 | n/a | n/a | n/a | sentinel-p3live-planner-badmodel: unhealthy, 5/5 kept checks failed; sentinel-p3live-planner: healthy, 2/5 kept checks failed |
| worker-container-planner-be6e636-20261112t181036z | be6e636 | PASS | 88/88 | 89/89 | 89.6 | 19.1 | 62.0 | 126.0 | planner-container: unhealthy, 3/5 kept checks failed |
<!-- END GENERATED:worker-container -->

## Claims

From [evidence/claims.json](../evidence/claims.json). Quote a claim with its
scope caveat.

<!-- BEGIN GENERATED:claims -->
| Claim | Statement | Scope caveat | Bundles |
| --- | --- | --- | --- |
| C-CONNECTED-HARNESS | The connected Boutique walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) passed in 4/4 runs | One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. Harness-authored proposals, no model. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 4 |
| C-AGENT-WARM | qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix | Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded. | 3 |
| C-AGENT-ALL | Across every recorded agent-mode attempt, 26/45 reached RECOVERED | Every committed agent bundle, failures included: deliberate negative tests, older revisions, and catalog runs with labels pending, so unscored. A recovery counts only when the run passed expecting recovery; expected escalations and the unhealthy and unavailable-evidence demonstrations count as not recovered. The cartservice C1 attempt FAILED after model abstention: safe escalation, zero Executor writes and administrator cleanup, not recovery. Mostly invalid shipping PORT faults where ROLLBACK is correct; the set also includes R2, N1 and Chaos Mesh N2. Earlier approval was pre-authorised or automated auto-lab, with no approval requested on expected escalations; one R1 run on 17 Nov 2026 SGT records a person approving interactively through approval_http (C-AGENT-HUMAN-APPROVAL). One Windows/amd64 host with Docker Desktop and kind; provenance inferred for the 20 runs before 4afc879 and recorded for the 25 later runs. | 45 |
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
| C-EXECUTOR-WEDGE | Live Executor wedge harness, main's Executor before the reconcile fix and integration/phase1 after it: rv-race at d5b2ed9 FAILED: NEEDS_RECONCILIATION on 5/5 reconcile passes, second proposal not admitted; rv-race at 4afc879 PASS: SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE on 5/5 reconcile passes, second proposal APPLIED; delete-recreate at 4afc879 PASS: SUPERSEDED/TARGET_REPLACED on 5/5 reconcile passes, second proposal APPLIED | One run per scenario, each in a disposable namespace on the local kind cluster; Guard is not under test. The before run copied the harness from 4afc879 into a checkout of main d5b2ed9 and replaced its shared.log import with a no-op stub. Covers a moved resourceVersion (rv-race) and a replaced target (delete-recreate) only: a rejected write that leaves the same UID at the same resourceVersion is not covered here; at these revisions it stays NEEDS_RECONCILIATION. The P2.06 fence for it ran live later at 4139385 and 5409092 (C-EXECUTOR-WEDGE-FENCE). One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance inferred: the scenario summaries carry no provenance block, so each revision is the operator's statement of the checkout's HEAD at run time, recorded in the bundle MANIFEST basis. | 3 |
| C-EXECUTOR-WEDGE-FENCE | Live Executor wedge harness on integration/phase2, including the admission-deny scenario that needs the P2.06 fence: admission-deny at 4139385 FAILED: CONFLICT/FENCED on 5/5 reconcile passes, second proposal APPLIED; rv-race at 4139385 PASS: SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE on 5/5 reconcile passes, second proposal APPLIED; delete-recreate at 4139385 PASS: SUPERSEDED/TARGET_REPLACED on 5/5 reconcile passes, second proposal APPLIED; admission-deny at 5409092 PASS: CONFLICT/FENCED on 5/5 reconcile passes, second proposal APPLIED | One run per scenario per revision, each in a disposable namespace; Guard is not under test. In admission-deny a ValidatingAdmissionPolicy denies the Pod template change, so the write is rejected with the same UID at the same resourceVersion; with fencing on, a reconcile pass after the 30 s settle delay sends one metadata-only fence. The 4139385 admission-deny run is FAILED on one harness check only: it expected the fence to leave metadata.generation unchanged, but a Deployment bumps generation on an annotation change. Its other 11 checks passed (CONFLICT/FENCED, exactly one fence, the workload released, the second proposal APPLIED). The harness check was fixed in 17d4532 to compare the spec, and the rerun at 5409092 passed all 12 checks. Fencing is off by default in the Executor. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance inferred: the scenario summaries carry no provenance block, so each revision is the operator's statement of the clean checkout's HEAD at run time, recorded in the bundle MANIFEST basis. | 4 |
| C-CONNECTED-HARNESS-AUTOLAB | With approval through the authenticated approval API, the connected Boutique walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) passed in 1/1 runs | One run. Approval auto-lab: the harness approved through the authenticated approval API under the automated-lab identity, automated and labelled, with no person deciding. It shows the API path live, not a person approving; no interactive or dashboard (ui) approval has run live. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action. Harness-authored proposals, no model. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4139385 on integration/phase2, clean tree, 2026-11-11 UTC. | 1 |
| C-APPROVAL-REJECT | With every approval refused through the authenticated approval API, the run stopped with no approval granted and no Executor record for the refused rollback in 1/1 runs | One deliberate negative test (approval mode reject, an automated refusal, not a person): the connected walkthrough run is FAILED by design ('Operator did not approve ...; no Executor write attempted'), its summary records one refusal and no approval, and the bundle holds no execution record for the refused action; the harness then removed the injected fault as the lab administrator (operator_emergency_restore true), outside the Executor and not counted as recovery. The scripted negative cases that run before approval (missing approval, hard policy, stale state) still ran. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4139385 on integration/phase2, clean tree, 2026-11-11 UTC. | 1 |
| C-GUARD-REJECTION-P2 | At 4139385, Guard rejection of an approved but unnecessary rollback: 1/1 refused, 0 Executor writes | One run of one scenario: the service is healthy and already at its baseline. The approval here is the fixed file-backed lab record of the Guard-only refusal check (approval mode not-requested), which never reaches the Coordinator and proves a refusal only. Counts Executor patch attempts for the refused action. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4139385 on integration/phase2, clean tree, 2026-11-11 UTC. | 1 |
| C-AGENT-AUTOLAB-4139385 | At 4139385, qwen3:14b agent mode with auto-lab approval reached RECOVERED in 2/3 runs | Three consecutive runs, failure included. The failed run ended NEEDS_REVIEW with zero Executor writes: the model named the target's container ('<pod>/server') as the root-cause component and the off-target check escalated it (DIAGNOSIS_OFF_TARGET). That was a defect in the check, fixed in 5300394, which accepts a <pod>/<container> reference to the target's own container; after the failed run the harness then removed the injected fault as the lab administrator (operator_emergency_restore true), outside the Executor and not counted as recovery. The first run (602449) recovered on its second planning attempt: the Analyzer's call in attempt 1 failed after about 45 s and 2048 output tokens (ValueError, journal code INVALID_MODEL_OUTPUT) and the journal retried. Approval auto-lab: the harness approved through the authenticated approval API under the automated-lab identity, automated and labelled, with no person deciding. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action. Model qwen3:14b on the local Ollama server; each bundle's model-runtime.json records Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call in model-calls.json loaded in under 1 s; the preflight warm-up, also in model-runtime.json, is separate and was a cold load of 18.1 s in the first run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4139385 on integration/phase2, clean tree, 2026-11-11 UTC. | 3 |
| C-AGENT-FORCED-ESCALATION | Given a planning budget too short for any model call, agent mode escalated to NEEDS_REVIEW with no Executor record, as expected, in 1/1 runs | One deliberate negative test: planning_budget_seconds 1 and --expect escalated. The journal made three attempts (two retries) and delivered the fixed BUDGET_EXCEEDED escalation with zero model calls completed; no approval was requested (mode not-requested). It shows the escalation path, not a recovery; the harness then removed the injected fault as the lab administrator (operator_emergency_restore true), outside the Executor and not counted as recovery. One fault type (invalid shipping PORT). One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 4139385 on integration/phase2, clean tree, 2026-11-11 UTC. | 1 |
| C-AGENT-AUTOLAB-5409092 | At 5409092, qwen3:14b agent mode with auto-lab approval reached RECOVERED in 3/3 consecutive runs | Three runs only, after the off-target fix (5300394). Three earlier attempts at this revision failed before any experiment step because the Docker Desktop engine was down; they are listed in evidence/exclusions.json as infrastructure failures, not Sentinel results. Approval auto-lab: the harness approved through the authenticated approval API under the automated-lab identity, automated and labelled, with no person deciding. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action. Model qwen3:14b on the local Ollama server; each bundle's model-runtime.json records Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call in model-calls.json and every preflight warm-up loaded in under 1 s; the model was already loaded by the warm-up of an excluded attempt (16.2 s cold load, see evidence/exclusions.json). One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 5409092 on integration/phase2, clean tree, 2026-11-12 UTC. | 3 |
| C-AGENT-R1-870AA7D | With the code-owned decision context and the telemetry injection gate merged, the standard PORT fault (catalog R1) in agent mode: R1 at 870aa7d PASS x3: ROLLBACK decided by the model, 1 Executor write, RECOVERED, matched the pre-registered expected outcome, labels pending | Three runs only. The catalog labels are pending owner sign-off, so these are not scored trials; scenario-result.json keeps the harness's provisional score. Each run's report names shippingservice and its planning-status.json records decided_by model with no eligibility code. Approval auto-lab: the harness approved through the authenticated approval API under the automated-lab identity, automated and labelled, with no person deciding. Model qwen3:14b on the local Ollama server; each bundle's model-runtime.json records Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call in model-calls.json loaded in under 1 s; the preflight warm-up, also in model-runtime.json, is separate and was a cold load of 19.4 s in the first run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 870aa7d on integration/phase2, clean tree, 2026-11-12 UTC. | 3 |
| C-SCENARIO-LIVE-UNSCORED | Live catalog scenario runs, unscored with labels pending: R2 at 870aa7d FAILED: ESCALATE from a planning failure (ABSTAINED), 0 Executor writes, NEEDS_REVIEW, did not match the pre-registered expected outcome, labels pending; N1 at 870aa7d FAILED: ESCALATE from a planning failure (ABSTAINED), 0 Executor writes, NEEDS_REVIEW, did not match the pre-registered expected outcome, labels pending; R2 at dab3b3f PASS: ROLLBACK decided by the model, 1 Executor write, RECOVERED, matched the pre-registered expected outcome, labels pending; N1 at dab3b3f PASS: ESCALATE decided by the rule (TEMPLATE_ALREADY_BASELINE), 0 Executor writes, NEEDS_REVIEW, matched the pre-registered expected outcome, labels pending; R1 at dab3b3f PASS: ROLLBACK decided by the model, 1 Executor write, RECOVERED, matched the pre-registered expected outcome, labels pending | UNSCORED: the pre-registered labels in experiments/boutique/catalog.json await owner sign-off, so these runs say what happened and whether it matched the pre-registered expected outcome; they are not scored trials. One run per scenario per revision. At 870aa7d R2 and N1 both FAILED: the Reporter diagnosed shippingservice and a faulty configuration rollout (rejected-report.json) but also cited a metrics result with no findings, and the citation rule (every cited result must name the component) made it abstain. That was a defect, fixed in ded3dcc (a cited result that names no component is neutral) and merged in dab3b3f. ESCALATE is in R2's pre-registered acceptable list, but an ESCALATE from a planning failure scores no_decision, so the R2 run at 870aa7d did not match. The dab3b3f runs used that check with the older Reporter prompt wording; 784c91a aligned the prompt afterwards and has not run live. Live N1 showed ErrImageNeverPull with the Pod template at its baseline, and the node image was restored after each N1 run (node_image_restored true). N1 at dab3b3f reached its escalation on the second planning attempt: the Reporter's call in attempt 1 failed after about 45 s and 2048 output tokens (ValueError) and the journal retried. After the R2 and N1 runs at 870aa7d and the N1 run at dab3b3f the harness removed the injected fault as the lab administrator (operator_emergency_restore true), outside the Executor and not counted as recovery. N1 asks for no approval; R1 and R2 used auto-lab approval (automated-lab identity, no person). Model qwen3:14b on the local Ollama server; each bundle's model-runtime.json records Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call in model-calls.json and every preflight warm-up loaded in under 1 s. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 870aa7d and dab3b3f on integration/phase2, clean trees, 2026-11-12 UTC. | 5 |
| C-INJECTION-GATE-BENIGN | Agent runs with ordinary (non-adversarial) telemetry recorded no injection finding and no flagged log line in any collected logs-tool result in 8/8 runs | Every agent run at 870aa7d and dab3b3f, failures included (R1, R2 and N1). It shows only that the gate raised no false positive on these live logs. No adversarial log line was injected live, so the gate has not been exercised against an attack outside the offline corpus, and the remote Planner worker's gate has not run live. Model qwen3:14b on the local Ollama server; each bundle's model-runtime.json records Q4_K_M fully on the GPU (gpu_fraction 1.0) with keep_alive 30m, and every model call in model-calls.json loaded in under 1 s; the preflight warm-up, also in model-runtime.json, is separate and was a cold load of 19.4 s in the first run. One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 870aa7d and dab3b3f on integration/phase2, clean trees, 2026-11-12 UTC. | 8 |
| C-TRACE-FILE-679EF29 | With SENTINEL_LOG_FORMAT=json, scripts/trace_view.py rebuilt each run's timeline into one connected trace across the Observer, Coordinator, Planner, Guard, Executor and Verifier processes: S0 PASS: 1 trace, 54 spans over 4.5 s from observer.ingest, 0 spans whose parent never ended; S1a PASS: 1 trace, 55 spans over 9.2 s from observer.ingest, 1 span whose parent never ended | Two runs. File mode: a file-backed fake cluster, not Kubernetes; the deterministic synthetic planner, no model; separate OS processes on one Windows host with TLS, not separate machines. In S1a the Planner was killed at planner.mid_inference (exit 73) and restarted: its span never ends, because the process died before span.end, so the retry is the only worker.process span for planner.request and is not drawn as a sibling of the lost one. trace-waterfall.txt in each bundle is the trace_view output, attached from beside the run folder. A scan of both timelines found no token-named field and no bearer or lease_token text. Provenance inferred: run_scenarios stamped git_revision 679ef29 (integration/phase3) but no dirty flag, 2026-11-12 UTC. | 2 |
| C-DISTRIBUTED-KIND-0A8D4D7 | Kind-mode distributed scenarios with scratch operator tools at 0a8d4d7, after the health policy was relaxed: 12/12 matched their expectation (S1b 2/2, S1c 2/2, S2 2/2, S4 2/2, S5 2/2, S8 2/2); every invariant held in 12/12; 0 unobserved windows; 0 harness errors | Two runs per scenario (S1b, S1c, S2, S4, S5, S8), with the 60 s scenario deadline and Verifier policy distributed-kind-lab-2. SCRATCH tools that were never committed prepared the bundle, injected R1 and restored the baseline (each bundle's kind-wrapper.json is that wrapper's record); only the Sentinel code was at 0a8d4d7. The first attempt, under policy distributed-kind-lab-1 (3 samples, 10 s gap), FAILED I3 with the action still VERIFYING at the deadline and is counted in C-DISTRIBUTED-KIND-ALL. S1b r1 and S2 r2 also opened a duplicate incident during the Recreate rollout (Observer failure window 2 s), which the Guard rejected with zero writes. Kind mode: the Executors patched the real shippingservice Deployment through a restricted token, and R1 (an invalid PORT) was injected before each run and the baseline template restored after it. The Planner is the deterministic synthetic planner (no model); the Observer, Guard and Verifier are the real ones, with TLS between the processes. The scenarios are fault-injection checks against the harness's own expectations and invariants (I1-I6, X1-X4), not scored trials. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected): the Coordinator and every worker were separate OS processes on that host, with one kernel clock, one disk and loopback networking, so this is not a test across machines. Provenance inferred: run_scenarios stamped git_revision but no dirty flag; the clean tree is an operator statement, 2026-11-12 UTC. | 12 |
| C-DISTRIBUTED-KIND-BE6E636 | Kind-mode distributed scenarios with the committed kind tools at be6e636: 11/12 matched their expectation (S1b 2/2, S1c 2/2, S2 1/2, S4 2/2, S5 2/2, S8 2/2); every invariant held in 11/12; failed: S2 I5; 0 unobserved windows; 0 harness errors | Two runs per scenario, with the committed experiments/distributed/kind_bundle.py and the in-harness injection and restore, the 180 s kind default deadline and the 15 s Observer window; no duplicate incident was opened. S2 r2 FAILED: the Executor write committed and the template went back to baseline, but the Verifier ended FAILED and I5 found command 9 (verifier.request) delivered 8 times against MAX_ATTEMPTS 6. Its passes after the write came at about +1, +2.6, +5.1, +9.7, +18.3, +34.9, +67.5 and +127.7 s, spaced by the Coordinator's doubling retry back-off; once a gap passes the policy's 20 s max_sample_gap, a target that turns healthy late cannot collect 2 samples close enough together. That cause is inferred: the Verifier's own samples are not in the bundle, so whether the 60 s convergence grace or the 90 s timeout decided FAILED is not known (L-VERIFIER-RETRY-BACKOFF). It is not fixed. I5 has no margin in kind mode: the Verifier command took 5 or 6 delivery attempts in each of the 9 passing non-S8 runs (6 in S1b r2 and S4 r2). R1 became visible 1.4-1.5 s after injection in 8 runs and 20.2-21.7 s in S1c r2, S2 r1, S2 r2 and S8 r2; other sessions were running tests on the same host at the time, which may explain the slow runs but was not measured. Kind mode: the Executors patched the real shippingservice Deployment through a restricted token, and R1 (an invalid PORT) was injected before each run and the baseline template restored after it. The Planner is the deterministic synthetic planner (no model); the Observer, Guard and Verifier are the real ones, with TLS between the processes. The scenarios are fault-injection checks against the harness's own expectations and invariants (I1-I6, X1-X4), not scored trials. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected): the Coordinator and every worker were separate OS processes on that host, with one kernel clock, one disk and loopback networking, so this is not a test across machines. Provenance inferred: run_scenarios stamped git_revision be6e636 but no dirty flag; the clean worktree is an operator statement, 2026-11-12 UTC. | 12 |
| C-DISTRIBUTED-KIND-ALL | Every kind-mode distributed scenario run on 12 Nov 2026, failures included: 23/25 matched their expectation (S1b 4/5, S1c 4/4, S2 3/4, S4 4/4, S5 4/4, S8 4/4); every invariant held in 23/25; failed: S1b I3; S2 I5; 0 unobserved windows; 0 harness errors | The denominator holds the failed first attempt at 0a8d4d7 (policy distributed-kind-lab-1, I3 failed with the action VERIFYING at the 60 s deadline), the 12 runs with scratch tools at 0a8d4d7 (C-DISTRIBUTED-KIND-0A8D4D7) and the 12 runs with committed tools at be6e636, where S2 r2 failed I5 (C-DISTRIBUTED-KIND-BE6E636). Two sessions on one day; repeated runs share the host, the cluster and the target Deployment, so they are not independent trials. Kind mode: the Executors patched the real shippingservice Deployment through a restricted token, and R1 (an invalid PORT) was injected before each run and the baseline template restored after it. The Planner is the deterministic synthetic planner (no model); the Observer, Guard and Verifier are the real ones, with TLS between the processes. The scenarios are fault-injection checks against the harness's own expectations and invariants (I1-I6, X1-X4), not scored trials. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected): the Coordinator and every worker were separate OS processes on that host, with one kernel clock, one disk and loopback networking, so this is not a test across machines. Provenance inferred for every run. | 25 |
| C-SEAL-LIVE | Scenario S8 in kind mode, where the Coordinator withdraws a command a paused Executor holds and the other Executor seals the Deployment: 0a8d4d7 PASS: executor-b paused, executor-a sealed at fence epoch 2 (FENCED), the paused patch CONFLICT, 0 writes after release, 0 committed template writes, incident FAILED, HTTP 409 to executor-b: renew lease_not_current, result result_changed; 0a8d4d7 PASS: executor-a paused, executor-b sealed at fence epoch 2 (FENCED), the paused patch CONFLICT, 0 writes after release, 0 committed template writes, incident FAILED, HTTP 409 to executor-a: renew lease_not_current, result result_changed; be6e636 PASS: executor-b paused, executor-a sealed at fence epoch 2 (FENCED), the paused patch CONFLICT, 0 writes after release, 0 committed template writes, incident FAILED, HTTP 409 to executor-b: renew lease_not_current, result result_changed; be6e636 PASS: executor-b paused, executor-a sealed at fence epoch 2 (FENCED), the paused patch CONFLICT, 0 writes after release, 0 committed template writes, incident FAILED, HTTP 409 to executor-b: renew lease_not_current | Four runs, two per revision; the incident is expected to end FAILED (cause fence_result) and each did. Each seal was a metadata-only patch to the real shippingservice Deployment: metadata.generation went 138 to 139 and 151 to 152 at 0a8d4d7, and 177 to 178 and 180 to 181 at be6e636, with no template write, and the operator restore after each run raised it once more. The paused Executor's conditional patch was refused (CONFLICT) and the write watcher counted 0 writes after release. The Coordinator answered the paused Executor's later calls with HTTP 409 lease_not_current on renew and, where it sent its result, result_changed; in be6e636 r2 the result_changed 409 went to guard-1 instead, and the paused executor-b logged a lost lease and retried. No 'stale epoch' answer was seen. The P3.05 admission policy was not installed during these runs, so they do not show a seal under the policy (see C-ADMISSION-POLICY-LIVE); that it was absent is an operator record, because the ValidatingAdmissionPolicy listings saved beside each session are not in the bundles. The seal leaves sentinel.io/fence and sentinel.io/fence-epoch on the Deployment and the restore puts back only the template; the operator removed them afterwards with a uid- and resourceVersion-tested metadata patch kept in the kind-lab-session bundles. The fence annotation is recorded for both 0a8d4d7 runs (kind-wrapper.json, post and final snapshots) and for be6e636 r2 (state/pre-cleanup-shippingservice.json in the kind-lab-session bundle); only be6e636 r1's annotation was overwritten, by r2's seal, before any snapshot. 0a8d4d7 r2 started on a Deployment already fenced at epoch 2 by r1 (kind-wrapper.json pre) and was sealed again at epoch 2. Synthetic planner. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected): the Coordinator and every worker were separate OS processes on that host, with one kernel clock, one disk and loopback networking, so this is not a test across machines. Provenance inferred, 2026-11-12 UTC. | 4 |
| C-ADMISSION-POLICY-LIVE | The P3.05 Executor admission policy, evaluated by a real API server with a restricted Executor token: 24/24 probes as expected on Kubernetes 1.37 (6 allowed, 12 denied by the policy, all 12 with the rule named and the object unchanged, 6 denied by RBAC); namespace, policy and private credentials removed | One run of scripts/validation/test_executor_admission_live.py in a disposable namespace (sentinel-admission-a957fc8f) on kind-sentinel-boutique-connected, with the policy bound to that namespace only; it is not installed in the Boutique lab namespaces, and the kind-mode scenario runs ran without it. Allowed through the production KubectlDeploymentAPI: a real ROLLBACK, a real RESTART, seals at epoch 5, 6 (higher) and 6 (equal), and an Executor fence without an epoch. Denied by the policy with the rule named and the object unchanged: replicas, spec.paused, a label, a foreign annotation changed or added, the fence annotation removed, a template change without new evidence, a seal that changes the template, a lower epoch, a malformed epoch, a malformed epoch with no stored epoch, and a seal at epoch 1 from a fresh Coordinator store ('may not decrease'), which the adapter reported as WriteOutcomeUnknown. That last probe confirms live the cost recorded in L-EXECUTOR-FIELD-LIMITS: a new or restored store cannot seal a Deployment fenced earlier at a higher epoch. Denied by RBAC: kubectl scale, get secrets, delete and create a Deployment (server dry run), and pods/exec create and get (auth can-i no). The namespace, the policy and the private credentials were removed afterwards. One kind node on one Windows host. Provenance inferred: branch feat/executor-admission-policy at 599f44f (since merged into integration/phase3), operator statement, 2026-11-12 UTC. | 1 |
| C-CART-TARGET-LIVE | The connected walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) on the second recovery target, cartservice with fault C1 (REDIS_ADDR set to a port nothing listens on), passed end to end in 1/2 runs | Two runs; C1's labels are pending owner sign-off, so these are not scored trials. The first run, at b79fb5d, FAILED with only status FAILED and integration_error RuntimeError in its bundle: every check its summary.json lists passed, and it has no dashboard-validation.json. The reason, the harness's dashboard acceptance check ('Dashboard must show actual downstream shopping failures'), which still expected shipping's impact (home working), comes from the run's console output, an operator record that is not in the bundle. Every Sentinel check in it passed, including two RECOVERED rollbacks, the crash reconciliation without resubmission and the Guard's ROLLBACK_ALREADY_AT_BASELINE rejection with zero Executor patch attempts. The check was fixed in a932a5f, and the rerun at a932a5f passed every check. Both runs showed the impact the catalog predicts for C1: home, add to cart, cart and checkout all answered 500, so C1 stays the primary cart fault and C2 was not needed. These two runs used harness-authored proposals, no model; the later agent-mode C1 attempt escalated safely without recovery (C-AGENT-CART-C1). Approval auto-lab (automated-lab identity, no person). One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: b79fb5d and a932a5f, clean trees, 2026-11-12 UTC. | 2 |
| C-SHIPPING-REGRESSION-B79FB5D | With cartservice added to the connected lab (both targets in the Executor Role, cart on a Recreate rollout), the shipping walkthrough and an agent-mode R1 run on shippingservice passed in 2/2 runs | Two runs, one of each, as a regression check of the changed connected lab. The walkthrough used harness-authored proposals; the agent run used qwen3:14b, reached ROLLBACK and RECOVERED, and its R1 labels are pending, so it is not a scored trial. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action. Approval auto-lab (automated-lab identity, no person). One Windows/amd64 host with Docker Desktop and kind (cluster kind-sentinel-boutique-connected). Provenance recorded: b79fb5d, clean tree, 2026-11-12 UTC. | 2 |
| C-WORKER-CONTAINER-READINESS | An agent-mode Planner run from the worker image as a container against a Coordinator on the host, at be6e636: 88/88 health samples Docker healthy with /readyz 200 from the first Docker-healthy one until the Coordinator stopped (89 taken while it was up, 89/89 answering /readyz 200; longest planning pass 89.6 s, highest contact age 19.1 s); /readyz 503 62.0 s and Docker unhealthy 126.0 s after the Coordinator stopped | One run. Image sentinel-worker:p3live2 built from deploy/docker/worker.Dockerfile at be6e636 (revision label), read-only root, tmpfs /tmp, a named volume for /data, default --ready-seconds 60 and the image's HEALTHCHECK (interval 30 s, timeout 5 s, start period 60 s, retries 3). The real Observer path opened INC-3F6B8409B555 and the Planner (qwen3:14b on the host's Ollama over plain HTTP, allow_insecure_remote) saved a ROLLBACK proposal in one planning attempt; the Coordinator moved the incident to VALIDATING. No Guard, Executor or Verifier ran. The pass outlasted the 60 s readiness window and the container stayed ready, which the pre-fix image did not (C-WORKER-READINESS-BEFORE-FIX). Lease renewals are inferred, not logged: the Coordinator writes no info-level renew line, and the evidence is a single-attempt receipt finishing about 90 s after the claim under a 30 s lease. At 18:24:31, after the pass and while the Coordinator was up, the Planner logged a worker.status retry after a TimeoutError and /readyz contact age reached 8.4 and 19.1 s; /readyz stayed 200 and Docker stayed healthy. /readyz turns 503 about one readiness window after the Coordinator stops, but Docker's own status needs up to 3 more 30 s checks. Other sessions ran tests on the same host at the time. The Planner's Kubernetes credential was a 45-minute get-only token for one Deployment, held in memory only; the CA and server keys were deleted. One Windows/amd64 host with Docker Desktop: the container and the Coordinator share one machine. Provenance inferred: operator statement and the image's revision label, 2026-11-12 UTC. | 1 |
| C-WORKER-READINESS-BEFORE-FIX | Before 8bda4e3, Docker's record of the agent-mode Planner container and of a deliberately broken one: sentinel-p3live-planner-badmodel: unhealthy, 5/5 kept checks failed; sentinel-p3live-planner: healthy, 2/5 kept checks failed | One session: the Coordinator ran at 0a8d4d7 and the image was built from c973edd, because the worker image at 0a8d4d7 failed to build (an ImportError, listed under the excluded runs); the bundle id names the Coordinator's revision, while index.json lists the image's (c973edd). The working Planner finished one 128.7 s planning pass for INC-90ED7FECF863 and the Coordinator moved the incident to VALIDATING on its proposal (coordinator.stderr.log); that the proposal was a ROLLBACK is an operator record, because the bundle holds no planner database or agent report. Docker kept its last five health checks, and two of them failed during that pass because /readyz counted only the end of a pass as Coordinator contact (L-WORKER-READINESS-LONG-PASS, fixed in 8bda4e3). Docker never marked it unhealthy, because three failures in a row are needed. The broken container had an unreachable model URL: /readyz answered 503 prerequisites_failed, Docker marked it unhealthy and it claimed nothing, as intended. The scratch orchestrator kept no event or health sample record, so the readiness table shows the run as INCOMPLETE. One Windows/amd64 host with Docker Desktop. Provenance inferred: operator statement, 2026-11-12 UTC. | 1 |
| C-KIND-LAB-SESSIONS | Kind lab preparation for the kind-mode sessions: 0a8d4d7: Executor RBAC checks 6/6 as expected, no restore check; be6e636: Executor RBAC checks 6/6 as expected, the restore check returned the target healthy at its baseline template in 21.8 s | Two sessions. At 0a8d4d7 a scratch tool prepared the bundle; at be6e636 the committed kind_bundle prepare did. Six checks of the Executor token each time: get and patch shippingservice allowed; delete, patch paymentservice, get secrets and list pods refused. The be6e636 restore check put R1 on the target with the committed injection and then ran kind_bundle restore: generation 183 to 184, Available, template back at baseline (template_sha256 5434e0fd...ef26). The restore puts back only the Pod template: the S8 fence annotations and a changed sentinel.io/execution stayed until the operator removed them with a uid- and resourceVersion-tested metadata patch. Across the be6e636 session shippingservice went from generation 156 to 187 and its deployment.kubernetes.io/revision from 137 to 165 through about 28 rollouts; its template and sentinel.* annotations ended as they started. The bundles hold Deployment snapshots only (before both sessions, and after the be6e636 session). The namespace, RBAC, admission-policy and Docker inventories the operator saved before each session are not in the bundles, so statements about them, such as no ValidatingAdmissionPolicy being installed, are operator records. One kind node on one Windows host. Provenance inferred, 2026-11-12 UTC. | 2 |
| C-CHAOS-RECOVERY-C9A44CA | Chaos Mesh faults in guarded recovery (P4.03), first set at c9a44ca, unscored with labels pending: N2 at c9a44ca FAILED x5: RESTART decided by the rule, 1 Executor write, RECOVERED, the action matched the pre-registered expected outcome but the run FAILED: drift cell not refused (no Guard verdict, Executor CONFLICT), labels pending; N3 at c9a44ca PASS: ESCALATE decided by the rule, 0 Executor writes, NEEDS_REVIEW, matched the pre-registered expected outcome, labels pending; T1 at c9a44ca PASS: NO_INCIDENT from observer, 0 Executor writes, no incident, matched the pre-registered expected outcome, labels pending | UNSCORED: the N2, N3 and T1 labels in experiments/boutique/catalog.json await owner sign-off. Each N2 run planned RESTART by the code rule, made one Executor write, reached RECOVERED, passed attribution (new Pod not selected by the chaos, recovered before expiry, chaos still running) and saw the forced ROLLBACK refused with ROLLBACK_ALREADY_AT_BASELINE and zero writes, yet all five FAILED in the drift cell: the forced RESTART got CONFLICT from the Executor (the drift's rollout had moved the Deployment's resourceVersion) before any Guard check, so no RESTART_TEMPLATE_DRIFT refusal was shown; there was no write and the administrator restore succeeded. That harness race was fixed in f5ce9db and 0d09f2d (merged in 29e41f8; see C-CHAOS-RECOVERY-29E41F8). N3 ESCALATEd by the metric-only rule with zero writes and its forced RESTART was refused with RESTART_NOT_JUSTIFIED; T1 recorded NO_INCIDENT with a passing true-negative proof. Every chaos object was deleted and its cleanup confirmed. Code rule planning, no model; N2 used auto-lab approval (automated-lab identity, no person). The host was shared with other test runs at the time. Chaos Mesh 2.8.2 (a privileged chaos-daemon) and metrics-server chart 3.12.2 with --kubelet-insecure-tls (image pinned by digest in c9a44ca) were installed on this lab under the user's standing authorisation to run Phases 4-5; the project-owner approval the recipe asks for is not recorded. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: c9a44ca on integration/phase4, clean tree, 2026-11-13 UTC. | 7 |
| C-CHAOS-RECOVERY-29E41F8 | Chaos Mesh faults in guarded recovery (P4.03), second set at 29e41f8, unscored with labels pending: N2 at 29e41f8 PASS x5: RESTART decided by the rule, 1 Executor write, RECOVERED, matched the pre-registered expected outcome, labels pending; N2 at 29e41f8 PASS: ESCALATE decided by the rule (TEMPLATE_ALREADY_BASELINE), 0 Executor writes, NEEDS_REVIEW, did not match the pre-registered expected outcome, labels pending | UNSCORED: labels await owner sign-off. Five N2 runs with the drift-cell fix: each planned RESTART by the code rule, one Executor write, RECOVERED and attributed (about 380 s before the chaos expired), the forced ROLLBACK refused with ROLLBACK_ALREADY_AT_BASELINE and the forced RESTART during drift refused with RESTART_BUDGET_EXHAUSTED and RESTART_TEMPLATE_DRIFT, zero writes for both; the drift settled before the forced proposal and no stale resourceVersion retry was needed. The agent variant (qwen3:14b, 2 model calls) diagnosed shippingservice failing because the chaos pause image cannot start, but the action came from the code eligibility rule (TEMPLATE_ALREADY_BASELINE): ESCALATE, NEEDS_REVIEW, zero writes; the model's own action choice was not exercised (model_action null). ESCALATE is in N2's acceptable list, so the run passed with --expect escalated and scored acceptable, but matched_expected is false because RESTART is the correct answer; the administrator removed the fault after it (operator_emergency_restore true), outside the Executor. Five repeats share the host, cluster and target, so they are not independent trials. Chaos Mesh 2.8.2 (a privileged chaos-daemon) and metrics-server chart 3.12.2 with --kubelet-insecure-tls (image pinned by digest in c9a44ca) were installed on this lab under the user's standing authorisation to run Phases 4-5; the project-owner approval the recipe asks for is not recorded. One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 29e41f8 on integration/phase4, clean tree, 2026-11-13 UTC. | 6 |
| C-DUAL-FAULT-BASELINES-29E41F8 | Single-fault baselines for the P4.05 timing comparison (the connected walkthrough with shipping R1 and with cart C1 at 29e41f8) passed in 2/2 runs | One run per target, harness-authored proposals, auto-lab approval (automated-lab identity, no person); R1 and C1 labels are pending, so these are not scored trials. They are the single-fault side of the P4.05 comparison; N2's single-fault timing comes from C-CHAOS-RECOVERY-29E41F8. No pair run at this revision has a result: all eight pair runs at 29e41f8 stopped on a harness defect right after both faults were applied (listed in evidence/exclusions.json); the pair runs were repeated at dca7369 (C-DUAL-FAULT-PAIRS-DCA7369). One Windows/amd64 host with Docker Desktop and one kind node (cluster kind-sentinel-boutique-connected); containers on one host are not separate machines. Provenance recorded: 29e41f8, clean tree, 2026-11-13 UTC. | 2 |
| C-DISTRIBUTED-KIND-02D91E5 | Kind-mode distributed scenarios at 02d91e5, after the Verifier in-pass sampling fix and the kind restore annotation reset: 12/12 matched their expectation (S1b 2/2, S1c 2/2, S2 2/2, S4 2/2, S5 2/2, S8 2/2); every invariant held in 12/12; 0 unobserved windows; 0 harness errors | Two runs per scenario with the committed kind tools. The Verifier command was delivered once (verifier_attempts 1) in each of the 10 non-S8 runs, against 5 to 8 at be6e636 (L-VERIFIER-RETRY-BACKOFF); S8 ended FAILED with zero writes as expected (the seal). After every run the restore recorded annotations status reset: after each of the 10 non-S8 runs its record shows the prepared sentinel.io/execution put back; after each S8 run it shows only sentinel.io/fence and sentinel.io/fence-epoch (epoch 2) removed. Kind mode: the Executors patched the real shippingservice Deployment through a restricted token; R1 was injected before each run and the baseline restored after it. Synthetic planner (no model); real Observer, Guard and Verifier with TLS between the processes. Fault-injection checks against the harness's own expectations and invariants, not scored trials. Chaos Mesh and metrics-server were installed on the cluster at the time. The Coordinator and every worker were separate OS processes on one host, with one kernel clock, one disk and loopback networking, so this is not a test across machines. Provenance inferred: run_scenarios stamped git_revision 02d91e5 but no dirty flag, 2026-11-13 UTC. | 12 |
| C-KIND-LAB-SESSION-02D91E5 | Kind lab preparation for the 13 Nov 2026 kind-mode recheck: 02d91e5: Executor RBAC checks 6/6 as expected, the restore check returned the target healthy at its baseline template in 0.2 s | One session with the committed kind_bundle prepare at 02d91e5. Six checks of the Executor token: get and patch shippingservice allowed; delete, patch paymentservice, get secrets and list pods refused. The restore check is the final kind_bundle restore after the twelve runs: the target was already healthy at its baseline template (generation 271 unchanged) and its annotations unchanged, because each run's own restore had put them back. The bundle directory, which held the executor kubeconfig, was deleted; prepare.out lists its file names only. The operator note (NOTES.txt) that sentinel.io/race and sentinel.io/stale-probe were already on the target at prepare comes from lab.json target_annotations read during the session; lab.json was in the deleted bundle directory and is not attached, and prepare.out's sentinel_annotations_at_prepare names only the annotations the restore manages (sentinel.io/execution, sentinel.io/fence, sentinel.io/fence-epoch), so no attached file shows or rules out those two. One kind node on one Windows host. Provenance inferred, 2026-11-13 UTC. | 1 |
| C-SECURITY-NETPOL-LIVE | The P4.06 graph-derived NetworkPolicy matrix in the protected Boutique security lab passed in 1/1 runs | One run on a fresh sentinel-boutique-security cluster with Calico 3.33.0 at 02d91e5: 19 of 19 allowed edges open, 202 of 202 forbidden edges closed by timeout, no policy drift, no untested cell; the storefront journey and the existing shipping bypass, identity and outage probes passed. L4 rules keyed on the Pod label app, not authentication; mTLS and inspection still cover the shipping path only, and the connected recovery cluster applies none of these policies. The first attempt stopped before any probe on an operator environment fault (evidence/exclusions.json). One Windows/amd64 host with Docker Desktop and one kind node. Provenance recorded: 02d91e5, 2026-11-13 UTC. | 1 |
| C-DUAL-FAULT-PAIRS-DCA7369 | P4.05 two-fault pair runs on the connected Boutique at dca7369, both faults applied within 0.3 s of each other: shipping-config+cart-pod at dca7369 PASS x3: RECOVERED, ShopOutcome RECOVERED, Executor writes cartservice 1 and shippingservice 1, RESTART attributed, chaos cleaned up, labels pending; shipping-config+cart-pod with an Executor kill on shippingservice at dca7369 PASS: RECOVERED, ShopOutcome RECOVERED, Executor writes cartservice 1 and shippingservice 1, RESTART attributed, shippingservice worker killed after 1 write, reconciled APPLIED with 0 further writes, chaos cleaned up, labels pending; redis-pod+cart-config at dca7369 PASS x3: DEPENDENCY_DEADLOCK, ShopOutcome FAILED, Executor writes cartservice 0 and redis-cart 0, ordered recovery shown: no, chaos cleaned up, administrator restored cartservice to baseline after scoring (not a repair), labels pending; redis-pod+cart-config with an Executor kill on cartservice at dca7369 PASS: DEPENDENCY_DEADLOCK, ShopOutcome FAILED, Executor writes cartservice 0 and redis-cart 0, no cartservice write, so no worker was killed, ordered recovery shown: no, chaos cleaned up, administrator restored cartservice to baseline after scoring (not a repair), labels pending | Eight runs on 13 Nov 2026 between 12:09 and 12:38 UTC: three of each pair and one Executor-kill variant of each. The pair labels in experiments/boutique/catalog.json are not signed off (labels pending), so these runs are checked against the pre-registered expectations but are not scored trials. Proposals came from the code rule provider with a trusted live-template read (no model), and every approval was auto-lab (automated-lab identity, no person decided). One Windows/amd64 host with Docker Desktop, one kind node (cluster kind-sentinel-boutique-connected) and one harness process that drove both incidents and verified one target at a time, so the pair timings do not measure independent workers or machines. In all four redis-pod+cart-config runs the Guard refused both repairs with DEPENDENCY_BLOCKED, the refusals named each other and the run stopped with DEPENDENCY_DEADLOCK and no write: the catalog accepts that as a safe stop, but it shows no ordered recovery (ordered_recovery_shown false), and none of the three storefront journeys taken after it passed (ShopOutcome FAILED). After scoring, the harness's administrator cleanup returned cartservice's C1 configuration to its baseline in each of those runs; that is lab cleanup, not a Sentinel repair, and the check recorded in those bundles still uses the older wording 'put the remaining faulted configuration back'. The dependency pair's kill variant stopped before any write, so there was nothing to kill and it shows nothing about reconciliation; only the independent pair's kill variant killed a worker. Whether a NotReady caller should block its callee's repair is still an owner decision (L-CALLER-BLOCKS-CALLEE-REPAIR). The eight earlier pair runs at 29e41f8 failed on a harness defect and are in evidence/exclusions.json. Provenance recorded: dca7369, clean tree, 2026-11-13 UTC. | 8 |
| C-DISTRIBUTED-KIND-S8-0A33BCF | Phase 6 deployed S8 (revocation mid-patch, seal, holder PATCH refused) passed in 5/5 runs | Invariants held in 5/5. Every holder PATCH was refused 422 after the seal; revoked actions made zero template writes. The audit maximum of 1 template write per action includes separately approved later incidents. Four kind nodes on one Docker host (shared kernel, clock and disk), synthetic planner, harness approvals; unscored, owner sign-off pending. Provenance recorded at deployed source 0a33bcf, captured 16 Nov 2026 UTC (17 Nov Singapore). | 1 |
| C-DISTRIBUTED-KIND-S11-0A33BCF | Phase 6 deployed refusal checks S11a (Guard-refused), S11b (missing approval) and S11c (stale state) passed in 3/3 runs | One run each, all passed with every invariant holding and zero Executor writes for each refused action. S11b observed two leases without approval, then a separately approved action wrote once. S11a's audit extract was omitted by the per-file cap; its summary and records remain collected. Four kind nodes on one Docker host (shared kernel, clock and disk), synthetic planner, harness approvals; unscored, owner sign-off pending. Provenance recorded at deployed source 0a33bcf, captured 16 Nov 2026 UTC (17 Nov Singapore). | 1 |
| C-DISTRIBUTED-KIND-S10-0A33BCF | Phase 6 deployed S10 (duplicate delivery of the same action) passed in 1/2 runs | Both runs remain in the denominator; invariants held in 2/2, one template write per run. S10-r1-170532z held at executor.before_submit: the original delivery was redelivered (attempt 2), but the peer found resourceVersion moved and returned CONFLICT; the Coordinator re-planned, the replacement action wrote, and the harness expectation failed. S10-r1-173528z held at executor.after_patch_before_save: the peer adopted the committed write with EVIDENCE_MATCH and no second PATCH. Run 2 was judged by the harness committed in b8e8c42 against the same deployed images from 0a33bcf. Four kind nodes on one Docker host (shared kernel, clock and disk), synthetic planner, harness approvals; unscored, owner sign-off pending. Provenance recorded at deployed source 0a33bcf, captured 16 Nov 2026 UTC (17 Nov Singapore). | 1 |
| C-AGENT-HUMAN-APPROVAL | With a person approving interactively through the authenticated approval API, qwen3:14b agent R1 reached RECOVERED in 1/1 runs | First live run with a person approving: operator youzerwan typed the action ID; summary.json records human true, mode interactive and via approval_http. The model chose ROLLBACK for the invalid shipping PORT; one Executor write, RECOVERED. Dashboard UI approval has not run live. One run on one Windows/amd64 Docker host with the connected lab and qwen3:14b; R1 labels pending, so unscored. Provenance recorded: 19661f3, clean tree, 2026-11-16 UTC (17 Nov SGT). | 1 |
| C-AGENT-OUTCOME-VARIANTS | Agent R1 negative outcome variants matched the expected Verifier outcome and Coordinator state in 2/2 runs | One unhealthy run ended NOT_RECOVERED / FAILED after the lab administrator scaled adservice to zero following the target rollback; one unavailable-evidence run ended UNKNOWN / NEEDS_REVIEW with health collection disabled until the deadline, not a network outage. Both acceptance runs PASS, with one Executor rollback write each; neither counts as recovered. Approval auto-lab, no person deciding. One Windows/amd64 Docker host, connected lab, qwen3:14b; labels pending, so unscored. Provenance recorded: f30b596, clean tree, 2026-11-16 UTC (17 Nov SGT). | 2 |
| C-AGENT-CART-C1 | Agent cartservice C1: 0/1 recovered, 0 Executor writes; safe escalation only | One attempt, retained as FAILED: expected RECOVERED, but the model abstained after four completed calls, producing ESCALATE / NEEDS_REVIEW (ABSTAINED, no_decision). Per team decision D6 this is a safe escalation; it shows no agent recovery on cartservice. Zero Executor routes and writes; harness administrator cleanup is not recovery. Approval mode auto-lab was configured, with no repair reaching approval. One Windows/amd64 Docker host, connected lab, qwen3:14b; C1 labels pending, so unscored. Provenance recorded: f30b596, clean tree, 2026-11-16 UTC (17 Nov SGT). | 1 |
<!-- END GENERATED:claims -->
