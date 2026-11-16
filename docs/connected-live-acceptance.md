# Connected agent acceptance

These commands prepare the remaining live demonstrations for issues #56 and
#74. Run them sequentially from the repository root on the frozen revision,
with Docker, the dedicated connected lab and the configured Ollama model
available. `-Setup` creates the dedicated lab if absent. Each invocation writes
a fresh run folder under `data/experimentation/runs`; retain its printed path
and `summary.json`. The launcher restores the previous Kubernetes context.

Use an operator identity for the person at the terminal. At the approval
prompt, inspect the proposal and type its exact action ID. The authenticated
approval record and `operator_approval` in the summary must name that operator,
with `human: true`; `auto-lab` is a separate, explicitly automated mode.

```powershell
# Agent R1, interactive approval
.\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Scenario R1 -Approval interactive -Operator $env:USERNAME

# Post-repair unhealthy shop evidence: NOT_RECOVERED, Coordinator FAILED
.\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Scenario R1 -OutcomeVariant unhealthy -Approval interactive -Operator $env:USERNAME

# No post-repair health evidence until the deadline: UNKNOWN, Coordinator NEEDS_REVIEW
.\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Scenario R1 -OutcomeVariant unavailable-evidence -Approval interactive -Operator $env:USERNAME

# Actual Planner on the second target, cartservice C1
.\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Target cartservice -Scenario C1 -Approval interactive -Operator $env:USERNAME
```

For cart-specific negative coverage, append `-OutcomeVariant unhealthy` or
`-OutcomeVariant unavailable-evidence` to the C1 command. These options are also
available through `scripts/test_boutique_live.py` as `--outcome-variant`, with
`--agent --connected --target cartservice --scenario C1`. The variants support
R1 and C1 only and cannot combine with `--expect`, the dashboard, pairs or other
planning modes. Agent mode can propose ROLLBACK or ESCALATE; it cannot propose
RESTART or SCALE by design.

The unhealthy variant scales **adservice only** to zero after the approved
Executor repair, waits for the zero-replica rollout and collects the ordinary
target, storefront and dependency readings. Shop verification requires this
dependency even when the target itself is healthy. The lab administrator then
restores the dependency's original replica count and verifies readiness in a
`finally` block. Its UID and resource version constrain cleanup; a changed
identity or failed restore fails the run. These administrator writes are
recorded separately from the restricted Executor's counters. The variant does
not change Executor permissions or claim that the target repair failed.

The unavailable-evidence variant disables health collection for the full
verification window. Timestamped collection-unavailable records contain no
invented health, readiness or dependency values. The durable Verifier retains
its normal convergence policy and fixed deadline (currently 300 seconds).
This exercises a deliberately unavailable collector, not a live network outage.

`outcome.json`, `post-health.json` and the Coordinator keep their actual
verdicts. `outcome_expectation` in `summary.json` records expected and actual
outcome/state, IDs and `matched_expected`; `expected_outcome` identifies the
variant. A matching negative demonstration can pass acceptance without claiming
RECOVERED. An unexpected recovery, planning escalation, refusal, collection
exception or cleanup failure fails the run. The unhealthy variant also retains
the dependency snapshots and `outcome_injection.restored`.

Offline tests establish the wiring only. Complete live rehearsal, sanitised
evidence review, prompt/context provenance and component-owner acceptance on
the same revision remain necessary before closing #56 or #74. Catalog labels
remain unscored until their owners sign them off. These commands do not claim
protected traffic; that requires the separate protected recovery acceptance.

## Live results (17 Nov 2026)

Four agent runs on the connected lab, one Docker host, qwen3:14b; recorded
16 Nov UTC / 17 Nov SGT. Scenario labels remain pending, so all are unscored.

| Run | Bundle | Result |
| --- | --- | --- |
| R1, interactive approval | [boutique-live-20261116t182043z-1a0d96](../evidence/2026-11-16/boutique-live-20261116t182043z-1a0d96/summary.json) | First live run with a person approving: youzerwan typed the action ID; `human=true`, `mode=interactive`, `via=approval_http`; ROLLBACK, RECOVERED, PASS. |
| R1, unhealthy | [boutique-live-20261116t175115z-3b106e](../evidence/2026-11-16/boutique-live-20261116t175115z-3b106e/summary.json) | Auto-lab approval; ROLLBACK, NOT_RECOVERED, Coordinator FAILED; matched the expected negative outcome, acceptance PASS. |
| R1, unavailable-evidence | [boutique-live-20261116t175656z-bdb808](../evidence/2026-11-16/boutique-live-20261116t175656z-bdb808/summary.json) | Auto-lab approval; ROLLBACK, UNKNOWN, Coordinator NEEDS_REVIEW; matched the expected negative outcome, acceptance PASS. |
| Cartservice C1 | [boutique-live-20261116t174705z-aa3f81](../evidence/2026-11-16/boutique-live-20261116t174705z-aa3f81/summary.json) | Model abstained after four completed calls: ESCALATE, NEEDS_REVIEW, zero Executor writes; administrator cleanup; expected RECOVERED, so FAILED. |

Claims: C-AGENT-HUMAN-APPROVAL **1/1** person-approved recovery;
C-AGENT-OUTCOME-VARIANTS **2/2** matched negative outcomes;
C-AGENT-CART-C1 **0/1 recovered, 0 Executor writes**. The failed cart attempt
stays in the denominator and, per D6, supports safe escalation only.
Dashboard UI approval has not run live. Each run's `model-calls.json` records
`prompt_version` and retrieved runbook IDs with SHA-256 content hashes (#54).
