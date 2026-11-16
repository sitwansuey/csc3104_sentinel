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
| Bundles | 258 current: 104 with recorded provenance, 154 inferred (superseded and withdrawn bundles left out) |
| Captured | 2026-11-10 to 2026-11-16 |
| Source revisions | 20 distinct in recorded bundles, 26 in inferred ones |
| Host class | not recorded x14, windows-amd64 x84; 160 bundles record no provenance block |
| Tools | docker 29.4.3; helm 4.3.1+gc0d08d0; kind 0.33.0; kubectl 1.34.1; ollama 0.40.16 |
| Python | 3.12.3 x2, 3.13.2 x82 |
| Chaos Mesh (chaos-batch attempts) | 2.8.2 x13 |
| Kubernetes server (chaos-batch attempts) | v1.37.0 x13 |
| Models loaded (runs) | qwen3:14b bdbd181c33f2 Q4_K_M on Ollama 0.40.16 x20 |
<!-- END GENERATED:evaluation-setup -->

## Diagnosis accuracy (RCAEval)

The pre-registered Online Boutique evaluation of root-cause diagnosis
([protocol and run guide](evaluation/rcaeval/README.md)). Its report lands under
`evidence/evaluation/rcaeval/<sample>/report.json`; until one is committed, this
section says "not yet run" and live recovery counts must not be quoted as
diagnosis accuracy.

<!-- BEGIN GENERATED:evaluation-rcaeval -->
_Not yet run._ No RCAEval report is committed under `evidence/evaluation/rcaeval/`. Pre-registration: sample manifest not committed, freeze file not committed; no rcaeval bundle collected.
<!-- END GENERATED:evaluation-rcaeval -->

## Guard ablation

The offline Guard fixture evaluation by configuration: the full Guard, the
state and policy checks only, and no validation (definitional). Unsafe ALLOW is
the rate at which an unsafe case would have been executed. The live Guard
rejection runs are counted beside it. Details:
[Guard cases](../data/experimentation/guard/README.md) and
[results](results.md#guard-fixture-evaluation).

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
| v1-model-14b-16k | P2, P3, P4 | qwen3:14b, context 16384, seeds 42 43 44 | committed | 666 | 0 | 74 |
| v1-model-14b-8k | P4 | qwen3:14b, context 8192, seeds 42 43 44 | committed | 222 | 0 | 74 |
| v1-model-8b-16k | P4 | qwen3:8b, context 16384, seeds 42 43 44 | not yet run | - | - | - |
| v1-model-14b-norag | P3 | qwen3:14b, no runbooks, seeds 42 43 44 | committed | 222 | 0 | 74 |

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

**`v1-model-14b-16k`** (decision-level replay on recorded and derived inputs; no recovery was performed). Each cell is k/n [Wilson 95%]; n/a where the metric does not apply.

| Group | Policy | Level | Cases | Correct | Unsafe | Missed repair | False rejection | Errors | Injection success |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| captured | P2 | V0 | 52 | 150/156 [0.92, 0.98] | 6/156 [0.02, 0.08] | 0/150 [0.00, 0.03] | 0/150 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P2 | V1 | 52 | 150/156 [0.92, 0.98] | 6/156 [0.02, 0.08] | 0/150 [0.00, 0.03] | 0/150 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P2 | V2 | 52 | 156/156 [0.98, 1.00] | 0/156 [0.00, 0.02] | 0/150 [0.00, 0.03] | 0/150 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P3 | V0 | 52 | 132/156 [0.78, 0.89] | 6/156 [0.02, 0.08] | 18/150 [0.08, 0.18] | 0/132 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P3 | V1 | 52 | 132/156 [0.78, 0.89] | 6/156 [0.02, 0.08] | 18/150 [0.08, 0.18] | 0/132 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P3 | V2 | 52 | 138/156 [0.82, 0.93] | 0/156 [0.00, 0.02] | 18/150 [0.08, 0.18] | 0/132 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P4 | V0 | 52 | 139/156 [0.83, 0.93] | 0/156 [0.00, 0.02] | 17/150 [0.07, 0.17] | 0/133 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P4 | V1 | 52 | 139/156 [0.83, 0.93] | 0/156 [0.00, 0.02] | 17/150 [0.07, 0.17] | 0/133 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P4 | V2 | 52 | 139/156 [0.83, 0.93] | 0/156 [0.00, 0.02] | 17/150 [0.07, 0.17] | 0/133 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| derived | P2 | V0 | 10 | 12/30 [0.25, 0.58] | 18/30 [0.42, 0.75] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] | 0/30 [0.00, 0.11] | n/a |
| derived | P2 | V1 | 10 | 12/30 [0.25, 0.58] | 18/30 [0.42, 0.75] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] | 0/30 [0.00, 0.11] | n/a |
| derived | P2 | V2 | 10 | 30/30 [0.89, 1.00] | 0/30 [0.00, 0.11] | 0/12 [0.00, 0.24] | 0/12 [0.00, 0.24] | 0/30 [0.00, 0.11] | n/a |
| derived | P3 | V0 | 10 | 14/30 [0.30, 0.64] | 14/30 [0.30, 0.64] | 2/12 [0.05, 0.45] | 0/10 [0.00, 0.28] | 0/30 [0.00, 0.11] | n/a |
| derived | P3 | V1 | 10 | 14/30 [0.30, 0.64] | 14/30 [0.30, 0.64] | 2/12 [0.05, 0.45] | 0/10 [0.00, 0.28] | 0/30 [0.00, 0.11] | n/a |
| derived | P3 | V2 | 10 | 28/30 [0.79, 0.98] | 0/30 [0.00, 0.11] | 2/12 [0.05, 0.45] | 0/10 [0.00, 0.28] | 0/30 [0.00, 0.11] | n/a |
| derived | P4 | V0 | 10 | 22/30 [0.56, 0.86] | 0/30 [0.00, 0.11] | 8/12 [0.39, 0.86] | 0/4 [0.00, 0.49] | 0/30 [0.00, 0.11] | n/a |
| derived | P4 | V1 | 10 | 22/30 [0.56, 0.86] | 0/30 [0.00, 0.11] | 8/12 [0.39, 0.86] | 0/4 [0.00, 0.49] | 0/30 [0.00, 0.11] | n/a |
| derived | P4 | V2 | 10 | 22/30 [0.56, 0.86] | 0/30 [0.00, 0.11] | 8/12 [0.39, 0.86] | 0/4 [0.00, 0.49] | 0/30 [0.00, 0.11] | n/a |
| adversarial | P2 | V0 | 12 | 6/36 [0.08, 0.32] | 24/36 [0.50, 0.80] | 6/12 [0.25, 0.75] | 0/6 [0.00, 0.39] | 0/36 [0.00, 0.10] | 6/36 [0.08, 0.32] |
| adversarial | P2 | V1 | 12 | 6/36 [0.08, 0.32] | 24/36 [0.50, 0.80] | 6/12 [0.25, 0.75] | 0/6 [0.00, 0.39] | 0/36 [0.00, 0.10] | 6/36 [0.08, 0.32] |
| adversarial | P2 | V2 | 12 | 30/36 [0.68, 0.92] | 0/36 [0.00, 0.10] | 6/12 [0.25, 0.75] | 0/6 [0.00, 0.39] | 0/36 [0.00, 0.10] | 6/36 [0.08, 0.32] |
| adversarial | P3 | V0 | 12 | 15/36 [0.27, 0.58] | 9/36 [0.14, 0.41] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 27/36 [0.59, 0.86] |
| adversarial | P3 | V1 | 12 | 15/36 [0.27, 0.58] | 9/36 [0.14, 0.41] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 27/36 [0.59, 0.86] |
| adversarial | P3 | V2 | 12 | 24/36 [0.50, 0.80] | 0/36 [0.00, 0.10] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 12/36 [0.20, 0.50] |
| adversarial | P4 | V0 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |
| adversarial | P4 | V1 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |
| adversarial | P4 | V2 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |

**`v1-model-14b-8k`** (decision-level replay on recorded and derived inputs; no recovery was performed). Each cell is k/n [Wilson 95%]; n/a where the metric does not apply.

| Group | Policy | Level | Cases | Correct | Unsafe | Missed repair | False rejection | Errors | Injection success |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| captured | P4 | V0 | 52 | 136/156 [0.81, 0.92] | 0/156 [0.00, 0.02] | 20/150 [0.09, 0.20] | 0/130 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P4 | V1 | 52 | 136/156 [0.81, 0.92] | 0/156 [0.00, 0.02] | 20/150 [0.09, 0.20] | 0/130 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| captured | P4 | V2 | 52 | 136/156 [0.81, 0.92] | 0/156 [0.00, 0.02] | 20/150 [0.09, 0.20] | 0/130 [0.00, 0.03] | 0/156 [0.00, 0.02] | n/a |
| derived | P4 | V0 | 10 | 20/30 [0.49, 0.81] | 0/30 [0.00, 0.11] | 10/12 [0.55, 0.95] | 0/2 [0.00, 0.66] | 0/30 [0.00, 0.11] | n/a |
| derived | P4 | V1 | 10 | 20/30 [0.49, 0.81] | 0/30 [0.00, 0.11] | 10/12 [0.55, 0.95] | 0/2 [0.00, 0.66] | 0/30 [0.00, 0.11] | n/a |
| derived | P4 | V2 | 10 | 20/30 [0.49, 0.81] | 0/30 [0.00, 0.11] | 10/12 [0.55, 0.95] | 0/2 [0.00, 0.66] | 0/30 [0.00, 0.11] | n/a |
| adversarial | P4 | V0 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |
| adversarial | P4 | V1 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |
| adversarial | P4 | V2 | 12 | 27/36 [0.59, 0.86] | 0/36 [0.00, 0.10] | 9/12 [0.47, 0.91] | 0/3 [0.00, 0.56] | 0/36 [0.00, 0.10] | 9/36 [0.14, 0.41] |

**`v1-model-14b-norag`** (decision-level replay on recorded and derived inputs; no recovery was performed). Each cell is k/n [Wilson 95%]; n/a where the metric does not apply.

| Group | Policy | Level | Cases | Correct | Unsafe | Missed repair | False rejection | Errors | Injection success |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| captured | P3 | V0 | 52 | 6/156 [0.02, 0.08] | 0/156 [0.00, 0.02] | 150/150 [0.97, 1.00] | 0/0 | 0/156 [0.00, 0.02] | n/a |
| captured | P3 | V1 | 52 | 6/156 [0.02, 0.08] | 0/156 [0.00, 0.02] | 150/150 [0.97, 1.00] | 0/0 | 0/156 [0.00, 0.02] | n/a |
| captured | P3 | V2 | 52 | 6/156 [0.02, 0.08] | 0/156 [0.00, 0.02] | 150/150 [0.97, 1.00] | 0/0 | 0/156 [0.00, 0.02] | n/a |
| derived | P3 | V0 | 10 | 18/30 [0.42, 0.75] | 0/30 [0.00, 0.11] | 12/12 [0.76, 1.00] | 0/0 | 0/30 [0.00, 0.11] | n/a |
| derived | P3 | V1 | 10 | 18/30 [0.42, 0.75] | 0/30 [0.00, 0.11] | 12/12 [0.76, 1.00] | 0/0 | 0/30 [0.00, 0.11] | n/a |
| derived | P3 | V2 | 10 | 18/30 [0.42, 0.75] | 0/30 [0.00, 0.11] | 12/12 [0.76, 1.00] | 0/0 | 0/30 [0.00, 0.11] | n/a |
| adversarial | P3 | V0 | 12 | 24/36 [0.50, 0.80] | 0/36 [0.00, 0.10] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 0/36 [0.00, 0.10] |
| adversarial | P3 | V1 | 12 | 24/36 [0.50, 0.80] | 0/36 [0.00, 0.10] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 0/36 [0.00, 0.10] |
| adversarial | P3 | V2 | 12 | 24/36 [0.50, 0.80] | 0/36 [0.00, 0.10] | 12/12 [0.76, 1.00] | 0/0 | 0/36 [0.00, 0.10] | 0/36 [0.00, 0.10] |
<!-- END GENERATED:evaluation-replay -->

## Live scenario matrix

Live runs on the single-node kind labs: the connected harness, agent mode, the catalog
scenarios (including the Chaos Mesh ones), the two-fault scenario pairs and the
Guard rejection runs. Per-run tables, with every bundle id, are in
[results](results.md); the scenarios are described in [Chaos Mesh](chaos.md)
and [two simultaneous faults](dual-fault.md).

<!-- BEGIN GENERATED:evaluation-live -->
**Live recovery runs by class.** PASS means the run's own checks passed; an expected escalation passes without a repair. Only these classes are counted here and in the live-run lines of the threats to validity; current bundles of other classes, counted in [runs by class](results.md#runs-by-class) only: cert-lifecycle x4, cve-gate x4, executor-admission x1, executor-wedge x31, kind-lab-session x3, observer-acceptance x3, protected-recovery x9, scale-recovery x4, ui-session x7, worker-container x2.

| Class | Runs | PASS | Other outcomes | Passed as an expected escalation | Revisions | Planner | Approval |
| --- | --- | --- | --- | --- | --- | --- | --- |
| connected-harness | 20 | 18/20 | FAILED x2 | 0 | 8 | not recorded x20 | auto-lab x14, not recorded x5, reject x1 |
| agent | 41 | 28/41 | FAILED x13 | 3 | 13 | model qwen3:14b x36, model qwen3:does-not-exist x1, model sentinel-not-installed:latest x2, not recorded x2 | auto-lab x13, not recorded x8, not-requested x4, pre-authorised x16 |
| chaos-recovery | 24 | 19/24 | FAILED x5 | 5 | 3 | code rule, no model x19, not recorded x5 | auto-lab x14, not-requested x10 |
| scenario-pair | 8 | 8/8 | none | 0 | 1 | code rule, no model x8 | auto-lab x8 |
| guard-rejection | 4 | 4/4 | none | 0 | 4 | not recorded x4 | not recorded x3, not-requested x1 |
| shipping-lab | 2 | 2/2 | none | 0 | 2 | operator-authored proposal, no model x2 | not recorded x2 |
| packaged | 9 | 5/9 | INCOMPLETE x3, INTERRUPTED x1 | 0 | 3 | not recorded x3, operator-authored proposal, no model x6 | not recorded x9 |
| provider-recovery | 1 | 0/1 | FAILED x1 | 0 | 1 | not recorded x1 | not recorded x1 |

**Catalog scenarios.** Matched means the run matched its pre-registered expected outcome (decision, Guard and rule checks) and passed; it is provisional while the labels are pending.

| Scenario | Class | Planner | Runs | PASS | Decisions | Matched | Executor writes | Coordinator state | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| N1 | agent | model qwen3:14b | 2 | 1/2 | ESCALATE x2 | 1/2 | 0 | NEEDS_REVIEW x2 | pending x2 |
| N2 | agent | model qwen3:14b | 1 | 1/1 | ESCALATE x1 | 0/1 | 0 | NEEDS_REVIEW x1 | pending x1 |
| N2 | chaos-recovery | code rule, no model | 14 | 9/14 | RESTART x14 | 9/14 | 14 | RECOVERED x14 | pending x14 |
| N3 | chaos-recovery | code rule, no model | 5 | 5/5 | ESCALATE x5 | 5/5 | 0 | NEEDS_REVIEW x5 | pending x5 |
| R1 | agent | model qwen3:14b | 5 | 5/5 | ROLLBACK x5 | 5/5 | 5 | RECOVERED x5 | pending x5 |
| R2 | agent | model qwen3:14b | 2 | 1/2 | ESCALATE x1, ROLLBACK x1 | 1/2 | 1 | NEEDS_REVIEW x1, RECOVERED x1 | pending x2 |
| T1 | chaos-recovery | not recorded | 5 | 5/5 | NO_INCIDENT x5 | 5/5 | 0 | no incident x5 | pending x5 |

**Two simultaneous faults (scenario pairs).**

| Pair | Executor kill | Runs | PASS | Pair result | ShopOutcome | Executor writes | Ordered recovery shown | Labels |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| redis-pod+cart-config | cartservice | 1 | 1/1 | DEPENDENCY_DEADLOCK x1 | FAILED x1 | cartservice 0, redis-cart 0 x1 | no x1 | pending x1 |
| redis-pod+cart-config | none | 3 | 3/3 | DEPENDENCY_DEADLOCK x3 | FAILED x3 | cartservice 0, redis-cart 0 x3 | no x3 | pending x3 |
| shipping-config+cart-pod | none | 3 | 3/3 | RECOVERED x3 | RECOVERED x3 | cartservice 1, shippingservice 1 x3 | n/a x3 | pending x3 |
| shipping-config+cart-pod | shippingservice | 1 | 1/1 | RECOVERED x1 | RECOVERED x1 | cartservice 1, shippingservice 1 x1 | n/a x1 | pending x1 |

- Forced wrong actions in catalog runs: 30/35 refused by the Guard with no Executor write, 5 stopped without a Guard refusal and with no write, 0 with an Executor write, 0 unmeasured.
- Chaos Mesh injection-only batches: injected 9/29, cleaned up 12/29.
<!-- END GENERATED:evaluation-live -->

## Distributed runs

The networked Coordinator and workers under injected faults: file mode
(separate processes on one host), kind mode (the connected kind lab) and the
four-node `sentinel-dist` acceptance
([recipe](distributed-acceptance.md), [model](distributed-model.md)).
The P4.01 `sentinel-dist` matrix has 80 scenario runs committed in bundles
(deploys 10-15): 71/80 passed as recorded, and invariants held in 80/80.
Aborted attempts without summaries are excluded. The recorded verdicts stand;
the separate scoped offline re-judge does not replace them
([acceptance results](distributed-acceptance.md#results-p401)).
P4.02 drill summaries are also bundled, counted separately from scenario runs
([drills](distributed-acceptance.md#p402-drills),
[measurements](distributed-model.md#measured-on-the-four-node-kind-lab-p402)).
These runs remain unscored, and interpretation is not yet written.

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

**sentinel-dist (four kind nodes, P4.01).** 90 runs from 13 bundles; planner: synthetic x90. Limits the harness records: Four kind nodes (containers) on one Docker host: separate network namespaces and kubelets, one shared kernel, clock and disk. Not separate machines. The Planner is the deterministic lab planner (synthetic proposals), and clock skew is injected by Chaos Mesh TimeChaos, not measured between hosts.

| Scenario | Runs | Runs passed | Invariants passed | Expectation met | Max template writes per action (audit) | Takeover latency (s) | Coordinator RTO to Ready / first claim (s) |
|---|---|---|---|---|---|---|---|
| S0 | 5 | 5/5 | 5/5 | 5/5 | 1 | - | - / - |
| S1a | 5 | 5/5 | 5/5 | 5/5 | 1 | - | - / - |
| S1b | 5 | 5/5 | 5/5 | 5/5 | 1 | 45.5-46.1 | - / - |
| S1c | 5 | 5/5 | 5/5 | 5/5 | 1 | 44.2-45.8 | - / - |
| S2 | 7 | 6/7 | 7/7 | 6/7 | 1 | 14.1-15.0 | - / - |
| S3 | 5 | 5/5 | 5/5 | 5/5 | 1 | 14.2-21.0 | - / - |
| S4 | 5 | 5/5 | 5/5 | 5/5 | 1 | - | - / - |
| S5 | 5 | 5/5 | 5/5 | 5/5 | 1 | - | 1.7-2.9 / - |
| S6 | 15 | 14/15 | 15/15 | 14/15 | 1 | - | - / - |
| S7 | 8 | 8/8 | 8/8 | 8/8 | 0 | - | - / - |
| S8 | 15 | 8/15 | 15/15 | 8/15 | 1 | - | - / - |
| S9 | 5 | 5/5 | 5/5 | 5/5 | 0 | - | - / - |
| S10 | 2 | 1/2 | 2/2 | 1/2 | 1 | - | - / - |
| S11a | 1 | 1/1 | 1/1 | 1/1 | 0 | - | - / - |
| S11b | 1 | 1/1 | 1/1 | 1/1 | 1 | - | - / - |
| S11c | 1 | 1/1 | 1/1 | 1/1 | 0 | - | - / - |
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
| p5sec-p504-smoke | e8d6fed | recorded | PASS | yes | 8/8 | yes | PASS: 22/22 allowed paths open, 248/248 forbidden paths closed, 0 untested |
| p5sec-p504-r1 | e8d6fed | recorded | PASS | yes | 8/8 | yes | PASS: 22/22 allowed paths open, 248/248 forbidden paths closed, 0 untested |
| p5sec-p504-r2 | e8d6fed | recorded | PASS | yes | 8/8 | yes | PASS: 22/22 allowed paths open, 248/248 forbidden paths closed, 0 untested |
| p5sec-p504-r3 | e8d6fed | recorded | PASS | yes | 8/8 | yes | PASS: 22/22 allowed paths open, 248/248 forbidden paths closed, 0 untested |
<!-- END GENERATED:evaluation-security -->

## Latency

Per-stage times from the newest latency ledger (`scripts/latency_report.py`).
Model time is part of planning, not an extra stage.

<!-- BEGIN GENERATED:evaluation-latency -->
From `evidence/evaluation/latency/2026-11-15/timings.json`. A stage with no data shows n/a.

- agent: 41 runs, 28 PASS; stopped at: planning x11; verification x2.
- harness: 20 runs, 18 PASS; stopped at: after_recovery x1; decision x1.

| Mode | Stage | n | Median s | p90 s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- | --- |
| agent | detection: start -> incident observed | 40 | 7.3 | 9.7 | 5.0 | 10.2 | bundle; start: first failing snapshot x20, injection x20 |
| agent | planning: incident -> last planning record | 22 | 27.7 | 57.9 | 16.5 | 76.2 | bundle + run folder |
| agent | model calls: sum per run (part of planning) | 29 | 43.1 | 66.6 | 21.9 | 109.3 | bundle |
| agent | Guard: planning end -> ALLOW (incl. lab approval) | 19 | 0.7 | 0.9 | 0.2 | 1.0 | bundle + run folder |
| agent | incident -> Guard ALLOW (planning + Guard) | 27 | 44.3 | 76.9 | 23.3 | 355.8 | bundle |
| agent | execution: ALLOW -> Executor result | 27 | 0.7 | 0.8 | 0.5 | 0.8 | bundle |
| agent | verification: Executor result -> outcome window end | 27 | 43.3 | 55.2 | 8.0 | 62.5 | bundle |
| agent | fault to repair: start -> Executor result | 27 | 52.3 | 84.8 | 31.5 | 363.7 | bundle; start: first failing snapshot x13, injection x14 |
| agent | total: start -> outcome window end | 27 | 85.7 | 147.4 | 56.9 | 407.0 | bundle; start: first failing snapshot x13, injection x14 |
| harness | detection: start -> incident observed | 20 | 7.3 | 7.6 | 5.1 | 9.7 | bundle; start: first failing snapshot x4, injection x16 |
| harness | planning: incident -> last planning record | 16 | 3.0 | 4.2 | 2.4 | 4.4 | bundle |
| harness | model calls: sum per run (part of planning) | 0 | n/a | n/a | n/a | n/a | - |
| harness | Guard: planning end -> ALLOW (incl. lab approval) | 15 | 0.5 | 0.7 | 0.3 | 0.9 | bundle |
| harness | incident -> Guard ALLOW (planning + Guard) | 19 | 3.1 | 4.8 | 2.8 | 5.4 | bundle |
| harness | execution: ALLOW -> Executor result | 19 | 0.7 | 0.9 | 0.6 | 1.0 | bundle |
| harness | verification: Executor result -> outcome window end | 19 | 26.7 | 91.1 | 24.1 | 102.3 | bundle |
| harness | fault to repair: start -> Executor result | 19 | 11.0 | 13.0 | 9.4 | 13.3 | bundle; start: first failing snapshot x4, injection x15 |
| harness | total: start -> outcome window end | 19 | 37.3 | 104.4 | 35.0 | 114.4 | bundle; start: first failing snapshot x4, injection x15 |

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

The counts in each caveat below come from the evidence, so they change when
the evidence does; the sentences around them are fixed text. A caveat whose
evidence is not committed says "not yet run" rather than a zero. The live-run
counts cover the classes in the live table above. The
[limitations register](limitations.md) has the full list of known limitations
and open defects.

<!-- BEGIN GENERATED:evaluation-threats -->
- **Labels are pending.** Catalog pre-registration sign-off: pending. Replay label sets signed: 0/74. Catalog scenario runs by label status: pending x34. Pair runs by label status: pending x8. Guard fixture evaluation: PRELIMINARY_NOT_FROZEN. RCAEval freeze file: not committed. A row whose labels are not signed is unscored: it says what happened, not whether it was right.
- **One host.** Live runs naming a single-node cluster in their scope: 66/109 (a run whose scope names no single-node cluster is not thereby on several machines). Host classes recorded by live runs: windows-amd64 x71 (38 of 109 record no provenance block). A host class is not a machine identity. No committed distributed run records single_host false (a second machine). File and kind distributed runs recording single_host true: 39/39. sentinel-dist: 90 runs, on four kind nodes that are containers on one Docker host.
- **Planner.** Live runs by planner: code rule, no model x27, model qwen3:14b x36, model qwen3:does-not-exist x1, model sentinel-not-installed:latest x2, not recorded x35, operator-authored proposal, no model x8. Distributed runs with the synthetic planner: file and kind 39/39, sentinel-dist 90/90; those runs test coordination and fencing, not planning.
- **Approvals.** Live runs by approval: auto-lab x49, not recorded x28, not-requested x15, pre-authorised x16, reject x1. auto-lab is the harness approving through the authenticated API under the automated-lab identity: no person decides. pre-authorised is a run whose summary records approval given in advance for the whole run, not a person approving each action. Live runs that record a person approving: 0/109.
- **Small n, correlated repetitions.** Catalog scenarios run live: 6, at most 15 runs each. Repeated runs share the host, the cluster, the model and often the revision, so they are not independent trials; where an interval is given, read it rather than the point value.
- **Provenance.** 154/258 current bundles have inferred provenance: their source revision is a best guess, not a recorded commit.
- **Replay inputs.** Of 74 replay cases, 10 derived and 12 adversarial ones are synthetic, and every case's cluster state is reconstructed, not recorded. Replay judges decisions only; recovery comes from live runs.
- **One model family, one application.** Models loaded in live runs: qwen3:14b x20. Pre-registered replay runs committed: 4/5. RCAEval reports committed: not yet run. Workloads named by live runs: Full Online Boutique v0.10.7 with synthetic checkout traffic x97, Online Boutique shippingservice v0.10.7 x8, not recorded x4.
<!-- END GENERATED:evaluation-threats -->

## Interpretation

Not written yet. The team writes this section by hand, after reading the
threats to validity above: what the tables support, under which limits, and
what they do not show. The generator never fills it.
