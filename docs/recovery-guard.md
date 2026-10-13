# Recovery Guard

The Guard is an offline evaluator for `ROLLBACK` proposals. It returns a validated
`guard_result` with `ALLOW`, `REJECT` or `ESCALATE`, explicit reason codes, the
policy version, the checked live resource version and a UTC evaluation time. It
does not execute recovery actions. Other action types are rejected even when a
policy enables them, until their checks are implemented.

## Run the standalone demo

No Kubernetes cluster, external packages or teammate services are needed:

```bash
python scripts/demo_guard.py
python scripts/demo_guard.py --scenario stale-state
python scripts/demo_guard.py --scenario valid-approved --now 2026-10-10T12:00:40Z
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py" -v
```

The script resolves fixtures relative to its own path, so it also works when
invoked by absolute path from another directory. The default output is a JSON
object mapping scenario names to wire-format Guard results. A single scenario
prints only its Guard result.

| Scenario | Expected verdict at the default clock |
| --- | --- |
| `valid-approved` | `ALLOW` |
| `missing-approval` | `ESCALATE` |
| `forbidden-target` | `REJECT` |
| `forbidden-action` | `REJECT` |
| `stale-state` | `REJECT` |
| `tampered-baseline` | `REJECT` |
| `blocked-dependencies` | `REJECT` |
| `unknown-dependencies` | `ESCALATE` |

The demo uses a fixed clock, **2026-10-10T12:00:40Z**, to keep dated fixtures
reproducible. `--now` requires an aware UTC timestamp and lets you experiment
with time checks. The fake approval is dated `2026-10-10T12:00:35Z`; moving the
clock before that time invalidates it. All scenarios except `missing-approval`
receive a matching fabricated approval, including hard rejection cases. Inputs
are copied before each scenario; the sample files remain unchanged. These
development approvals have no authority outside the demo.

## Evaluator interface

```python
evaluate(proposal, live_state, policy, baseline, dependencies,
         approval=None, *, now=None)
```

`now` is an optional timezone-aware UTC `datetime`; omitting it uses current UTC.
The evaluator does not mutate its inputs. Proposal and dependency inputs use the
existing shared wire contracts. The following local input formats are temporary
adapter contracts; they do not add new shared message schemas.

| Input | Source and required meaning |
| --- | --- |
| `proposal` | A `proposal` wire message containing the exact target, action, baseline ID/hash and resource version used when planning |
| `live_state` | An observation-shaped object like `samples/observations.json["failed-rollout"][-1]`: `cluster_id`, UTC `captured_at`, and `deployment` with `kind` plus `metadata.namespace`, `name`, `uid`, `resourceVersion` |
| `policy` | `samples/policy.json`: `policy_id`, `policy_version`, exact target allowlist, action allowlist, boolean `require_operator_approval`, and `unknown_dependency_result` set to `ESCALATE` or `REJECT` |
| `baseline` | `samples/baseline.json`: exact target, baseline ID, lowercase template SHA-256, UTC `healthy_observed_at`, and saved `pod_template` |
| `dependencies` | A `dependency_result` wire message for the same incident, action and target |
| `approval` | Optional local operator record described below |

Target identity includes **cluster, namespace, kind, name and UID**. A Deployment
recreated with the same name is a different target. Resource versions are opaque
strings: compare them for equality, never parse or order them numerically.

Policies may optionally set `max_state_age_seconds` and
`max_dependency_age_seconds` to nonnegative integers. Without those settings,
there is no maximum age limit; future timestamps are still checked. A future or
over-age live snapshot is rejected. Future or over-age dependency results are
treated as unknown and follow `unknown_dependency_result`. Every policy change
must receive a new `policy_version` so old approvals cannot authorize new rules.

Malformed wire messages raise `shared.contracts.ContractError`. Malformed local
policy or live snapshots raise `GuardInputError`, a `ContractError` subclass.
A missing or malformed baseline produces `REJECT`; a malformed approval produces
`ESCALATE` after the hard checks pass. Callers must catch input errors as
evaluation failures and make no write. If the live
resource version is missing, the Guard raises an error; it cannot fabricate the
nonempty version required by the existing output contract.

## Baselines and operator approval

The first milestone rolls back to a saved healthy template. Both the saved
baseline hash and the proposal hash must match the recomputed template hash.
`template_sha256(pod_template)` and `proposal_fingerprint(proposal)` use SHA-256
over UTF-8 encoded JSON from:

```python
json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
```

This uses Python's default `ensure_ascii=True` and rejects nonfinite numbers.
The proposal fingerprint covers
the **entire proposal**, including rationale, evidence references, target,
parameters and resource version. Changing any field requires a new matching
approval; reusing only an action ID is insufficient.

A temporary local approval record looks like this:

```json
{
  "action_id": "ACT-DEMO-001",
  "proposal_sha256": "<proposal_fingerprint(proposal)>",
  "policy_version": "demo-policy-v1",
  "operator_id": "demo-operator",
  "decision": "APPROVED",
  "approved_at": "2026-10-10T12:00:35Z",
  "expires_at": "2026-10-10T12:05:35Z"
}
```

`expires_at` is optional. Timestamps must be aware UTC. Approval cannot be dated
in the future, and an approval expires at its expiry timestamp. An operator can
record `REJECTED` instead of `APPROVED`. Mismatched, malformed, future or expired
approval records cannot grant permission. Hard target, action, state, baseline
and dependency violations take precedence over approval. Unknown dependencies
cannot be cleared by approval.

When approval is optional, omitting the record is sufficient; a provided invalid
record still escalates, and a matching current `REJECTED` record rejects. Future
or expired records do not become authoritative rejections. A baseline with a
future healthy-observation timestamp is rejected.

The fixture baseline is assumed to come from a trusted healthy-baseline store.
Approval records are assumed to come from a trusted Coordinator with an
authenticated operator. Hashing detects payload changes; it does not authenticate
the sender or establish that a saved template was actually healthy. The
Coordinator will own persistence, operator authentication, revocation and
approval delivery when these interfaces are integrated.

## Integration responsibilities

Member 6 supplies dependency results; the Guard validates their linkage and
makes the final decision. `BLOCKED` or a nonempty blocker list prevents recovery.
`UNKNOWN` or unresolved unknowns follow the configured unknown-result policy.
Missing required operator approval escalates an otherwise safe proposal.

Member 1 supplies trusted approval records bound to the exact proposal and policy
version. Agree on this temporary record's transport and revocation rules before
using it across services. Member 5 rereads live state and asks the Guard to
evaluate again immediately before writing, under per-target coordination. A
stale-version rejection requires fresh observation and revalidation, potentially
replanning and renewed approval. Conditional Kubernetes writes protect against
changes between that check and the write; an `ALLOW` result does not freeze live
state or provide distributed coordination.

Next increments are live-state and baseline adapters, agreed approval persistence
and freshness limits, integration with Coordinator/Executor/dependency producers,
resource constraints for additional actions, and a service runtime/deployment.
Recovery previews remain optional. This fixture evaluator is one component of
the offline milestone, not a complete autonomous recovery service.

## Reason codes

`REJECT` takes precedence over `ESCALATE`; rejected results contain only their
hard rejection reasons. An allowed result contains `VALIDATION_PASSED`.

| Check | Reason codes |
| --- | --- |
| Policy | `TARGET_NOT_ALLOWED`, `ACTION_NOT_ALLOWED`, `ACTION_NOT_IMPLEMENTED` |
| Live identity/version | `LIVE_TARGET_MISMATCH`, `TARGET_UID_MISMATCH`, `STALE_RESOURCE_VERSION` |
| Live timestamps | `LIVE_STATE_FROM_FUTURE`, `STALE_LIVE_STATE` |
| Baseline | `BASELINE_MISSING`, `INVALID_BASELINE`, `BASELINE_TARGET_MISMATCH`, `BASELINE_ID_MISMATCH`, `BASELINE_HASH_MISMATCH`, `BASELINE_FROM_FUTURE` |
| Dependency linkage/blockers | `DEPENDENCY_RESULT_MISMATCH`, `DEPENDENCY_BLOCKED` |
| Unknown dependency information | `DEPENDENCY_UNKNOWN`, `DEPENDENCY_RESULT_FROM_FUTURE`, `STALE_DEPENDENCY_RESULT` |
| Approval requiring review | `OPERATOR_APPROVAL_REQUIRED`, `INVALID_APPROVAL`, `APPROVAL_MISMATCH`, `APPROVAL_FROM_FUTURE`, `APPROVAL_EXPIRED` |
| Current matching operator denial | `OPERATOR_REJECTED` |
