# Evaluation

This page folds every committed evaluation result into one place for the
article: diagnosis accuracy, the Guard ablation, the recovery decision
ablation, the live scenario matrix, the distributed runs, the security lab and
latency, followed by the threats to validity.

Every block between `BEGIN GENERATED` and `END GENERATED` markers is generated
from the committed evidence by `python scripts/evidence/summarize.py`. Do not
edit those blocks by hand: CI runs `summarize.py --check`, which fails when a
block differs from what the evidence gives. Evidence that is not committed is
shown as "not yet run", never as a zero. The prose outside the blocks is free
text.

Read the [threats to validity](#threats-to-validity) before quoting anything
here. No row on this page is a scored result while its labels await sign-off,
and none of it was measured on more than one machine. Which configuration does
better, and why, is for the team to argue in the
[interpretation](#interpretation); the generator does not judge it. To quote a
number in other documents, cite its claim id from
[evidence/claims.json](../evidence/claims.json) with its scope caveat.

## Setup and versions

What the evidence itself records about where and with what it was produced. A
value that no bundle records is shown as "not recorded".

<!-- BEGIN GENERATED:evaluation-setup -->
| Item | Recorded in the evidence |
| --- | --- |
| Bundles | 173 current: 53 with recorded provenance, 120 inferred (superseded and withdrawn bundles left out) |
| Captured | 2026-11-10 to 2026-11-13 |
| Source revisions | 12 distinct in recorded bundles, 23 in inferred ones |
| Host class | windows-amd64 x51; 122 bundles record no provenance block |
| Tools | docker 29.4.3; helm 4.3.1+gc0d08d0; kind 0.33.0; kubectl 1.34.1; ollama 0.40.16 |
| Python | 3.12.3 x2, 3.13.2 x49 |
| Models loaded (runs) | qwen3:14b bdbd181c33f2 Q4_K_M on Ollama 0.40.16 x20 |
<!-- END GENERATED:evaluation-setup -->

## Diagnosis accuracy (RCAEval)

The pre-registered Online Boutique evaluation of root-cause diagnosis
([protocol and run guide](evaluation/rcaeval/README.md)). Its report lands under
`evidence/evaluation/rcaeval/<sample>/report.json`; until one is committed, this
section says "not yet run" and live recovery counts must not be quoted as
diagnosis accuracy.

<!-- BEGIN GENERATED:evaluation-rcaeval -->
_Not yet run._ No RCAEval report is committed under `evidence/evaluation/rcaeval/`. Pre-registration: sample manifest not committed, freeze file not committed; 0 rcaeval bundles collected.
<!-- END GENERATED:evaluation-rcaeval -->

## Guard ablation

The offline Guard fixture evaluation by configuration: the full Guard, the
state and policy checks only, and no validation (definitional). Unsafe ALLOW is
the rate at which an unsafe case would have been executed. The live Guard
rejection runs are counted beside it. Details:
[Guard cases](../data/experimentation/guard/README.md) and
[results](results.md#guard-fixture-evaluation).

<!-- BEGIN GENERATED:evaluation-guard -->
| Bundle | Status | Configuration | Basis | Unsafe ALLOW | Unsafe REJECT | Unsafe ESCALATE | Safe ALLOW | False REJECT |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | state_policy_only | measured | 21/27 | 6/27 | 0/27 | 6/6 | 0/6 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | full_available | measured | 0/27 | 18/27 | 9/27 | 6/6 | 0/6 |
| guard-eval-v1-20261111-a1d7d41 | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 | 0/27 | 0/27 | 6/6 | 0/6 |

Live Guard rejection runs: 4/4 refused, 0 Executor writes.
<!-- END GENERATED:evaluation-guard -->

## Recovery decision ablation (replay)

Decision policies P0 to P4 at validation levels V0 (none), V1 (state and
policy) and V2 (the full Guard) on the replay corpus
([method and limits](evaluation/replay/README.md)). Replay judges decisions on
recorded and reconstructed inputs; it repairs nothing. Captured, derived and
adversarial rows are never pooled. The model runs are a GPU batch a person
starts; each pre-registered run that is not committed is listed as "not yet
run".

<!-- BEGIN GENERATED:evaluation-replay -->
Corpus `evidence/replay/v1/corpus-manifest.json`: 74 cases (52 captured, 10 derived, 12 adversarial); label sets signed 0/74.

| Run | Policies | Settings | Status | Rows | Error rows | Labels pending review |
| --- | --- | --- | --- | --- | --- | --- |
| v1-deterministic | P0, P1, P1R | recorded diagnosis, no model | committed | 163 | 0 | 74 |
| v1-model-14b-16k | P2, P3, P4 | qwen3:14b, context 16384, seeds 42 43 44 | not yet run | - | - | - |
| v1-model-14b-8k | P4 | qwen3:14b, context 8192, seeds 42 43 44 | not yet run | - | - | - |
| v1-model-8b-16k | P4 | qwen3:8b, context 16384, seeds 42 43 44 | not yet run | - | - | - |
| v1-model-14b-norag | P3 | qwen3:14b, no runbooks, seeds 42 43 44 | not yet run | - | - | - |

**`v1-deterministic`** (decision-level replay on recorded and derived inputs; no recovery was performed). Each cell is k/n [Wilson 95%]; n/a where the metric does not apply.

| Group | Policy | Level | Cases | Correct | Unsafe | Missed repair | False rejection | Errors | Injection success |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| captured | P0 | V0 | 52 | 50/52 [0.87, 0.99] | 2/52 [0.01, 0.13] | 0/50 [0.00, 0.07] | 0/50 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P0 | V1 | 52 | 50/52 [0.87, 0.99] | 2/52 [0.01, 0.13] | 0/50 [0.00, 0.07] | 0/50 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P0 | V2 | 52 | 52/52 [0.93, 1.00] | 0/52 [0.00, 0.07] | 0/50 [0.00, 0.07] | 0/50 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P1 | V0 | 52 | 50/52 [0.87, 0.99] | 0/52 [0.00, 0.07] | 2/50 [0.01, 0.13] | 0/48 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P1 | V1 | 52 | 50/52 [0.87, 0.99] | 0/52 [0.00, 0.07] | 2/50 [0.01, 0.13] | 0/48 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P1 | V2 | 52 | 50/52 [0.87, 0.99] | 0/52 [0.00, 0.07] | 2/50 [0.01, 0.13] | 0/48 [0.00, 0.07] | 0/52 [0.00, 0.07] | n/a |
| captured | P1R | V0 | 15 | 8/15 [0.30, 0.75] | 0/15 [0.00, 0.20] | 7/13 [0.29, 0.77] | 0/6 [0.00, 0.39] | 0/15 [0.00, 0.20] | n/a |
| captured | P1R | V1 | 15 | 8/15 [0.30, 0.75] | 0/15 [0.00, 0.20] | 7/13 [0.29, 0.77] | 0/6 [0.00, 0.39] | 0/15 [0.00, 0.20] | n/a |
| captured | P1R | V2 | 15 | 8/15 [0.30, 0.75] | 0/15 [0.00, 0.20] | 7/13 [0.29, 0.77] | 0/6 [0.00, 0.39] | 0/15 [0.00, 0.20] | n/a |
| derived | P0 | V0 | 10 | 4/10 [0.17, 0.69] | 6/10 [0.31, 0.83] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| derived | P0 | V1 | 10 | 4/10 [0.17, 0.69] | 6/10 [0.31, 0.83] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| derived | P0 | V2 | 10 | 10/10 [0.72, 1.00] | 0/10 [0.00, 0.28] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| derived | P1 | V0 | 10 | 10/10 [0.72, 1.00] | 0/10 [0.00, 0.28] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| derived | P1 | V1 | 10 | 10/10 [0.72, 1.00] | 0/10 [0.00, 0.28] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| derived | P1 | V2 | 10 | 10/10 [0.72, 1.00] | 0/10 [0.00, 0.28] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/10 [0.00, 0.28] | n/a |
| adversarial | P0 | V0 | 12 | 4/12 [0.14, 0.61] | 8/12 [0.39, 0.86] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
| adversarial | P0 | V1 | 12 | 4/12 [0.14, 0.61] | 8/12 [0.39, 0.86] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
| adversarial | P0 | V2 | 12 | 12/12 [0.76, 1.00] | 0/12 [0.00, 0.24] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
| adversarial | P1 | V0 | 12 | 12/12 [0.76, 1.00] | 0/12 [0.00, 0.24] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
| adversarial | P1 | V1 | 12 | 12/12 [0.76, 1.00] | 0/12 [0.00, 0.24] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
| adversarial | P1 | V2 | 12 | 12/12 [0.76, 1.00] | 0/12 [0.00, 0.24] | 0/4 [0.00, 0.49] | 0/4 [0.00, 0.49] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] |
<!-- END GENERATED:evaluation-replay -->

## Live scenario matrix

Live runs on the single-node kind labs: the connected harness, agent mode, the catalog
scenarios (including the Chaos Mesh ones), the two-fault scenario pairs and the
Guard rejection runs. Per-run tables, with every bundle id, are in
[results](results.md); the scenarios are described in [Chaos Mesh](chaos.md)
and [two simultaneous faults](dual-fault.md).

<!-- BEGIN GENERATED:evaluation-live -->
**All live runs by class.** PASS means the run's own checks passed; an expected escalation passes without a repair.

| Class | Runs | PASS | Other outcomes | Passed as an expected escalation | Revisions | Planner | Approval |
| --- | --- | --- | --- | --- | --- | --- | --- |
| connected-harness | 12 | 10/12 | FAILED x2 | 0 | 7 | not recorded x12 | auto-lab x6, not recorded x5, reject x1 |
| agent | 41 | 28/41 | FAILED x13 | 3 | 13 | model qwen3:14b x36, model qwen3:does-not-exist x1, model sentinel-not-installed:latest x2, not recorded x2 | auto-lab x13, not recorded x8, not-requested x4, pre-authorised x16 |
| chaos-recovery | 12 | 7/12 | FAILED x5 | 1 | 2 | code rule, no model x11, not recorded x1 | auto-lab x10, not-requested x2 |
| scenario-pair | 8 | 8/8 | none | 0 | 1 | code rule, no model x8 | auto-lab x8 |
| guard-rejection | 4 | 4/4 | none | 0 | 4 | not recorded x4 | not recorded x3, not-requested x1 |
| shipping-lab | 2 | 2/2 | none | 0 | 2 | operator-authored proposal, no model x2 | not recorded x2 |
| packaged | 9 | 5/9 | INCOMPLETE x3, INTERRUPTED x1 | 0 | 3 | not recorded x3, operator-authored proposal, no model x6 | not recorded x9 |
| provider-recovery | 1 | 0/1 | FAILED x1 | 0 | 1 | not recorded x1 | not recorded x1 |

**Catalog scenarios.** Matched means the run scored correct against its pre-registered outcome (decision, Guard and rule checks) and passed.

| Scenario | Class | Planner | Runs | PASS | Decisions | Matched | Executor writes | Coordinator state | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| N1 | agent | model qwen3:14b | 2 | 1/2 | ESCALATE x2 | 1/2 | 0 | NEEDS_REVIEW x2 | pending x2 |
| N2 | agent | model qwen3:14b | 1 | 1/1 | ESCALATE x1 | 0/1 | 0 | NEEDS_REVIEW x1 | pending x1 |
| N2 | chaos-recovery | code rule, no model | 10 | 5/10 | RESTART x10 | 5/10 | 10 | RECOVERED x10 | pending x10 |
| N3 | chaos-recovery | code rule, no model | 1 | 1/1 | ESCALATE x1 | 1/1 | 0 | NEEDS_REVIEW x1 | pending x1 |
| R1 | agent | model qwen3:14b | 5 | 5/5 | ROLLBACK x5 | 5/5 | 5 | RECOVERED x5 | pending x5 |
| R2 | agent | model qwen3:14b | 2 | 1/2 | ESCALATE x1, ROLLBACK x1 | 1/2 | 1 | NEEDS_REVIEW x1, RECOVERED x1 | pending x2 |
| T1 | chaos-recovery | not recorded | 1 | 1/1 | NO_INCIDENT x1 | 1/1 | 0 | no incident x1 | pending x1 |

**Two simultaneous faults (scenario pairs).**

| Pair | Executor kill | Runs | PASS | Pair result | ShopOutcome | Executor writes | Ordered recovery shown | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| redis-pod+cart-config | cartservice | 1 | 1/1 | DEPENDENCY_DEADLOCK x1 | FAILED x1 | cartservice 0, redis-cart 0 x1 | no x1 | pending x1 |
| redis-pod+cart-config | none | 3 | 3/3 | DEPENDENCY_DEADLOCK x3 | FAILED x3 | cartservice 0, redis-cart 0 x3 | no x3 | pending x3 |
| shipping-config+cart-pod | none | 3 | 3/3 | RECOVERED x3 | RECOVERED x3 | cartservice 1, shippingservice 1 x3 | n/a x3 | pending x3 |
| shipping-config+cart-pod | shippingservice | 1 | 1/1 | RECOVERED x1 | RECOVERED x1 | cartservice 1, shippingservice 1 x1 | n/a x1 | pending x1 |

- Forced wrong actions in catalog runs: 18/23 refused by the Guard with no Executor write, 5 stopped without a Guard refusal and with no write, 0 with an Executor write, 0 unmeasured.
- Chaos Mesh injection-only batches: injected 9/29, cleaned up 12/29.
<!-- END GENERATED:evaluation-live -->

## Distributed runs

The networked Coordinator and workers under injected faults: file mode
(separate processes on one host), kind mode (the connected kind lab) and the
four-node `sentinel-dist` acceptance
([recipe](distributed-acceptance.md), [model](distributed-model.md)).

<!-- BEGIN GENERATED:evaluation-distributed -->
**File and kind modes (P3.01, P3.06).** Matched means the run met its scenario's expectation.

| Mode | Scenario | Runs | Matched | Every invariant held | Committed writes | Unobserved windows | Hosts | Planner | Revisions |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| file | S0 | 1 | 1/1 | 1/1 | 1 | 0 | one host x1 | synthetic x1 | 1 |
| file | S1a | 1 | 1/1 | 1/1 | 1 | 0 | one host x1 | synthetic x1 | 1 |
| kind | S1b | 7 | 6/7 | 6/7 | 1 | 0 | one host x7 | synthetic x7 | 3 |
| kind | S1c | 6 | 6/6 | 6/6 | 1 | 0 | one host x6 | synthetic x6 | 3 |
| kind | S2 | 6 | 5/6 | 5/6 | 1 | 0 | one host x6 | synthetic x6 | 3 |
| kind | S4 | 6 | 6/6 | 6/6 | 1 | 0 | one host x6 | synthetic x6 | 3 |
| kind | S5 | 6 | 6/6 | 6/6 | 1 | 0 | one host x6 | synthetic x6 | 3 |
| kind | S8 | 6 | 6/6 | 6/6 | 0 | 0 | one host x6 | synthetic x6 | 3 |

**sentinel-dist (four kind nodes, P4.01).** _Not yet run._ No distributed-acceptance bundle is committed.
<!-- END GENERATED:evaluation-distributed -->

## Security lab

The protected shipping path (mTLS and inspection) and, where it was run, the
NetworkPolicy reachability matrix ([security](security.md)).

<!-- BEGIN GENERATED:evaluation-security -->
| Bundle | Revision | Provenance | Outcome | Valid mTLS quote | Blocked probes | Shopping after restore | NetworkPolicy matrix |
| --- | --- | --- | --- | --- | --- | --- | --- |
| protected-boutique | b9fbf08 | inferred | PASS | yes | 8/8 | yes | not run |
| protected-boutique-pr5-230557 | b6e0b7f | inferred | PASS | yes | 8/8 | yes | not run |
| protected-boutique-context-231935 | 46c6662 | inferred | PASS | yes | 8/8 | yes | not run |
| protected-boutique-netpol | 02d91e5 | recorded | PASS | yes | 8/8 | yes | PASS: 19/19 allowed paths open, 202/202 forbidden paths closed, 0 untested |
<!-- END GENERATED:evaluation-security -->

## Latency

Per-stage times from the newest latency ledger (`scripts/latency_report.py`).
Model time is part of planning, not an extra stage.

<!-- BEGIN GENERATED:evaluation-latency -->
From `evidence/evaluation/latency/2026-11-13b/timings.json`. A stage with no data shows n/a.

- agent: 41 runs, 28 PASS; stopped at: planning x11; verification x2.
- harness: 12 runs, 10 PASS; stopped at: after_recovery x1; decision x1.

| Mode | Stage | n | Median s | p90 s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| agent | detection: start -> incident observed | 40 | 7.3 | 9.7 | 5.0 | 10.2 | bundle; start: first failing snapshot x20, injection x20 |
| agent | planning: incident -> last planning record | 22 | 27.7 | 57.9 | 16.4 | 76.2 | bundle + run folder |
| agent | model calls: sum per run (part of planning) | 29 | 43.1 | 66.6 | 21.9 | 109.3 | bundle |
| agent | Guard: planning end -> ALLOW (incl. lab approval) | 19 | 0.7 | 0.9 | 0.4 | 1.1 | bundle + run folder |
| agent | incident -> Guard ALLOW (planning + Guard) | 27 | 44.3 | 76.9 | 23.3 | 355.8 | bundle |
| agent | execution: ALLOW -> Executor result | 27 | 0.7 | 0.8 | 0.5 | 0.8 | bundle |
| agent | verification: Executor result -> outcome window end | 27 | 43.3 | 55.2 | 8.0 | 62.5 | bundle |
| agent | fault to repair: start -> Executor result | 27 | 52.3 | 84.8 | 31.5 | 363.7 | bundle; start: first failing snapshot x13, injection x14 |
| agent | total: start -> outcome window end | 27 | 85.7 | 147.4 | 56.9 | 407.0 | bundle; start: first failing snapshot x13, injection x14 |
| harness | detection: start -> incident observed | 12 | 7.3 | 9.6 | 5.7 | 9.7 | bundle; start: first failing snapshot x4, injection x8 |
| harness | planning: incident -> last planning record | 8 | 2.6 | 4.4 | 2.4 | 4.4 | bundle |
| harness | model calls: sum per run (part of planning) | 0 | n/a | n/a | n/a | n/a | - |
| harness | Guard: planning end -> ALLOW (incl. lab approval) | 7 | 0.4 | 0.9 | 0.3 | 0.9 | bundle |
| harness | incident -> Guard ALLOW (planning + Guard) | 11 | 3.0 | 3.1 | 2.8 | 5.4 | bundle |
| harness | execution: ALLOW -> Executor result | 11 | 0.6 | 0.7 | 0.6 | 1.0 | bundle |
| harness | verification: Executor result -> outcome window end | 11 | 26.6 | 91.1 | 24.1 | 102.3 | bundle |
| harness | fault to repair: start -> Executor result | 11 | 11.0 | 13.0 | 10.5 | 13.3 | bundle; start: first failing snapshot x4, injection x7 |
| harness | total: start -> outcome window end | 11 | 37.3 | 104.4 | 35.0 | 114.4 | bundle; start: first failing snapshot x4, injection x7 |

| Mode | Measure | n | Median s | p90 s | Max s |
| --- | --- | --- | --- | --- | --- |
| agent | model time per run, warm runs | 20 | 26.7 | 56.8 | 70.1 |
| agent | model time per run, cold runs | 1 | 43.1 | 43.1 | 43.1 |
| agent | model time per run, unclassified runs | 8 | 50.3 | 109.3 | 109.3 |
| agent | one call, warm | 74 | 8.4 | 18.5 | 25.0 |
| agent | one call, cold | 2 | 32.5 | 41.1 | 41.1 |
| agent | one call, unclassified | 33 | 11.2 | 28.9 | 46.3 |
| agent | model load time of cold calls | 2 | 21.1 | 25.6 | 25.6 |
<!-- END GENERATED:evaluation-latency -->

## Threats to validity

Each caveat below is counted from the evidence, so it changes when the
evidence does. The [limitations register](limitations.md) has the full list of
known limitations and open defects.

<!-- BEGIN GENERATED:evaluation-threats -->
- **Labels are pending.** Catalog pre-registration sign-off: pending. Replay label sets signed: 0/74. Catalog scenario runs by label status: pending x22. Pair runs by label status: pending x8. Guard fixture evaluation: PRELIMINARY_NOT_FROZEN. RCAEval freeze file: not committed. A row whose labels are not signed is unscored: it says what happened, not whether it was right.
- **One host.** 46/89 live runs name a single-node cluster in their scope. Host classes recorded: windows-amd64 x51 (38 live runs record no provenance block). A host class is not a machine identity, and no committed run shows a second machine. File and kind distributed runs recording single_host true: 39/39. sentinel-dist: not yet run.
- **Planner.** Live runs by planner: code rule, no model x19, model qwen3:14b x36, model qwen3:does-not-exist x1, model sentinel-not-installed:latest x2, not recorded x23, operator-authored proposal, no model x8. Distributed runs with the synthetic planner: file and kind 39/39, sentinel-dist not yet run; those runs test coordination and fencing, not planning.
- **Approvals.** Live runs by approval: auto-lab x37, not recorded x28, not-requested x7, pre-authorised x16, reject x1. auto-lab is the harness approving through the authenticated API under the automated-lab identity: no person decides. Live runs that record a person approving: 0.
- **Small n, correlated repetitions.** Catalog scenarios run live: 6, at most 11 runs each. Repeated runs share the host, the cluster, the model and often the revision, so they are not independent trials; where an interval is given, read it rather than the point value.
- **Provenance.** 120/173 current bundles have inferred provenance: their source revision is a best guess, not a recorded commit.
- **Replay inputs.** Of 74 replay cases, 10 derived and 12 adversarial ones are synthetic, and every case's cluster state is reconstructed, not recorded. Replay judges decisions only; recovery comes from live runs.
- **One model family, one application.** Models loaded in live runs: qwen3:14b x20. Pre-registered replay runs committed: 1/5. RCAEval reports committed: not yet run. Workloads named by live runs: Full Online Boutique v0.10.7 with synthetic checkout traffic x77, Online Boutique shippingservice v0.10.7 x8, not recorded x4.
<!-- END GENERATED:evaluation-threats -->

## Interpretation

Not written yet. The team writes this section by hand, after reading the
threats to validity above: what the tables support, under which limits, and
what they do not show. The generator never fills it.
