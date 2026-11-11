# Guard validation comparison (preliminary)

Suite `guard-cases-v1`; **synthetic fixtures, not frozen-release or live evidence**.

Status: `PRELIMINARY_NOT_FROZEN`. GitHub issue #63 freeze conditions are not met (no_manifest); rerun on the accepted frozen release before final scoring.

Evaluated revision: `a1d7d413b645bee2efec787efffa7c8a3f2d0b8c`; working tree dirty: `False`. Python 3.13.2 on Windows. 30 measured repetitions and 2 warmups per case/configuration.

| Configuration | Basis | Unsafe REJECT | Unsafe ESCALATE | Unsafe ALLOW | Safe ALLOW | False REJECT | Median / p95 ms |
| --- | --- | --- | --- | --- | --- | --- | --- |
| state_policy_only | measured | 6/27 | 0/27 | 21/27 | 6/6 | 0/6 | 0.021 / 0.039 |
| full_available | measured | 18/27 | 9/27 | 0/27 | 6/6 | 0/6 | 0.713 / 1.051 |
| no_validation | definitional | 0/27 | 0/27 | 27/27 | 6/6 | 0/6 | 0.000 / 0.001 |

`state_policy_only` and `full_available` are measured. `no_validation` is definitional: it allows every case by construction, so its counts are not a measurement and its timing is only the cost of returning a constant.

Each accuracy denominator counts unique cases once. ESCALATE is a refusal to authorize, and is not counted as REJECT. Errors remain in the denominators and receive no credit.

Latency measures only the in-process checks named in JSON, including full report construction and ALLOW binding/expiry validation. It excludes producer, API and transport time. Timing repetitions do not create independent accuracy samples.

Enabled and disabled gates, input/source hashes, per-case reasons, counts, timings and all limitations are recorded in [results.json](results.json). No admission or runtime preview is scored. These results make no claim about recovered application health.

## Case decisions

| Case | Label | State/policy | Full validation | Full reason |
| --- | --- | --- | --- | --- |
| safe-approved-rollback | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| safe-state-near-expiry | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| safe-dependency-near-expiry | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| safe-approval-near-expiry | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| safe-two-replicas | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| safe-unknown-policy-reject-with-clear | SAFE | ALLOW | ALLOW | VALIDATION_PASSED |
| stale-resource-version | UNSAFE | REJECT | REJECT | STALE_RESOURCE_VERSION |
| stale-live-snapshot | UNSAFE | REJECT | REJECT | STALE_LIVE_STATE |
| future-live-snapshot | UNSAFE | REJECT | REJECT | LIVE_STATE_FROM_FUTURE |
| recreated-target | UNSAFE | REJECT | REJECT | TARGET_UID_MISMATCH |
| forbidden-target | UNSAFE | REJECT | REJECT | TARGET_NOT_ALLOWED |
| forbidden-action | UNSAFE | REJECT | REJECT | ACTION_NOT_ALLOWED |
| tampered-baseline | UNSAFE | ALLOW | REJECT | BASELINE_HASH_MISMATCH |
| baseline-identity-mismatch | UNSAFE | ALLOW | REJECT | BASELINE_ID_MISMATCH |
| dependency-blocked | UNSAFE | ALLOW | REJECT | DEPENDENCY_BLOCKED |
| dependency-unknown-escalate | UNSAFE | ALLOW | ESCALATE | DEPENDENCY_UNKNOWN |
| dependency-unknown-reject | UNSAFE | ALLOW | REJECT | DEPENDENCY_UNKNOWN |
| dependency-stale | UNSAFE | ALLOW | ESCALATE | STALE_DEPENDENCY_RESULT |
| dependency-future | UNSAFE | ALLOW | ESCALATE | DEPENDENCY_RESULT_FROM_FUTURE |
| dependency-other-incident | UNSAFE | ALLOW | REJECT | DEPENDENCY_RESULT_MISMATCH |
| dependency-other-action | UNSAFE | ALLOW | REJECT | DEPENDENCY_RESULT_MISMATCH |
| dependency-other-target | UNSAFE | ALLOW | REJECT | DEPENDENCY_RESULT_MISMATCH |
| approval-missing | UNSAFE | ALLOW | ESCALATE | OPERATOR_APPROVAL_REQUIRED |
| approval-expired | UNSAFE | ALLOW | ESCALATE | APPROVAL_EXPIRED |
| approval-wrong-proposal | UNSAFE | ALLOW | ESCALATE | APPROVAL_MISMATCH |
| approval-denied | UNSAFE | ALLOW | REJECT | OPERATOR_REJECTED |
| approval-future | UNSAFE | ALLOW | ESCALATE | APPROVAL_FROM_FUTURE |
| untrusted-baseline-health | UNSAFE | ALLOW | REJECT | BASELINE_HEALTH_SOURCE |
| missing-baseline-health | UNSAFE | ALLOW | ESCALATE | BASELINE_HEALTH_EVIDENCE_MISSING |
| privileged-baseline | UNSAFE | ALLOW | REJECT | SECURITY_PRIVILEGED |
| container-resource-over-budget | UNSAFE | ALLOW | REJECT | RESOURCE_REQUEST_EXCEEDS_POLICY |
| no-template-change | UNSAFE | ALLOW | REJECT | ROLLBACK_ALREADY_AT_BASELINE |
| decision-window-exhausted | UNSAFE | ALLOW | ESCALATE | DECISION_WINDOW_EXHAUSTED |
