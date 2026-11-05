# Recovery Guard

The Guard is connected to the coordinator through the durable local worker in
`runtime/guard_pipeline.py`. `python scripts/coordinator.py guard --help` shows
file-input and read-only Kubernetes input options. See
[Coordinator integration](coordinator.md#recovery-guard-worker) for approval,
dependency and restart behavior. `scripts/demo_coordinator.py` exercises this
worker; `tests/test_guard_integration.py` covers the real decision/routing paths.

The Guard evaluates `ROLLBACK` proposals. Its decision core is offline and a
[command-line runner](guard-runtime.md) can supply files or read live Deployment
state using an explicit Kubernetes context. It returns a validated
version-2 `guard_result` with `ALLOW`, `REJECT` or `ESCALATE`, explicit reason
codes, policy version, checked live resource version, UTC evaluation time,
`proposal_sha256`, `inputs_sha256` and `valid_until`. It
does not execute recovery actions. Other action types are rejected even when a
policy enables them, until their checks are implemented.

See the [version-2 guide](guard-v2.md) for baseline health/age, configuration
compatibility, security, whole-workload resource budgets, admission previews and
detailed audit reports. The ownership section below explains what the Guard
owner contributes and where teammates supply trusted inputs.

## Ownership (Member 4)

The Guard owner maintains the rules between a suggested recovery and the
Executor's cluster write:

- Maintain versioned, workload-configured policies and explain ALLOW, REJECT
  and ESCALATE decisions, with hard constraints taking precedence over approval.
- Maintain the expiring proposal/input fingerprints and reports consumed by the
  Coordinator and Executor; new dependency evidence must revoke stale permission.
- Test invalid/stale identity, unsafe templates, resource constraints, missing
  evidence and approval failures, including zero writes after refusal.
- Record expected decisions, actual reasons, input fingerprints and latency for
  reproducible evaluation; distinguish synthetic evaluation from live evidence.
- Coordinate trusted state with Observer, proposal/baseline identity with
  Planner, approval/lifecycle with Coordinator and dependency/health evidence
  with Verifier. The Executor independently requests fresh Guard validation.

These local handoffs now run in the [connected Boutique experiment](boutique-connected.md).
Its proposals are harness-authored; real Planner/RCA and protected recovery
remain separate integration work. See the [integration contract](guard-integration-contract.md)
for the detailed owner boundaries. Code alone does not establish authenticated
provenance or named-person agreement.

## Run the standalone demo

No Kubernetes cluster, external packages or teammate services are needed:

```bash
python scripts/demo_guard.py
python scripts/demo_guard_v2.py
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
         approval=None, *, now=None, preview=None)
evaluate_with_report(proposal, live_state, policy, baseline, dependencies,
                     approval=None, *, now=None, preview=None)
```

`now` is an optional timezone-aware UTC `datetime`; omitting it uses current UTC.
The evaluator does not mutate its inputs. Proposal and dependency inputs use the
existing shared wire contracts. The following local input formats are temporary
adapter contracts; they do not add new shared message schemas.
`evaluate_with_report` returns `(decision, report)`; the report lists individual
checks and input fingerprints. Other wire-message kinds remain version 1.
Shared validation accepts historical version-1 Guard results for reading, while
`assert_decision_usable` requires a trusted version-2 `ALLOW`, exact input binding
and an unexpired validity interval. A fingerprint is not authentication or a
signed execution credential.

| Input | Source and required meaning |
| --- | --- |
| `proposal` | A `proposal` wire message containing the exact target, action, baseline ID/hash and resource version used when planning |
| `live_state` | An observation-shaped object like `data/sentinel/samples/observations.json["failed-rollout"][-1]`: `cluster_id`, UTC `captured_at`, and `deployment` with `kind` plus `metadata.namespace`, `name`, `uid`, `resourceVersion` |
| `policy` | `data/sentinel/samples/policy.json`: `policy_id`, `policy_version`, exact target allowlist, action allowlist, boolean `require_operator_approval`, and `unknown_dependency_result` set to `ESCALATE` or `REJECT` |
| `baseline` | `data/sentinel/samples/baseline.json`: exact target, baseline ID, lowercase template SHA-256, UTC `healthy_observed_at`, and saved `pod_template` |
| `dependencies` | A `dependency_result` wire message for the same incident, action and target |
| `approval` | Optional local operator record described below |
| `preview` | Bound server-side admission result when required; offline fixtures may replay a separately supplied record |

Target identity includes **cluster, namespace, kind, name and UID**. A Deployment
recreated with the same name is a different target. Resource versions are opaque
strings: compare them for equality, never parse or order them numerically.

Development policies may optionally set `max_state_age_seconds` and
`max_dependency_age_seconds` to nonnegative integers. Without those settings,
there is no maximum age limit; future timestamps are still checked. A future or
over-age live snapshot is rejected. Future or over-age dependency results are
treated as unknown and follow `unknown_dependency_result`. Every policy change
must receive a new `policy_version` so old approvals cannot authorize new rules.
Live reads require a strict `profile: "live"` policy before any cluster request;
freshness limits, bounded approval, baseline evidence, security and workload
constraints become mandatory. The [version-2 guide](guard-v2.md) lists the fields.

Malformed wire messages raise `shared.contracts.ContractError`. Malformed local
policy or live snapshots raise `GuardInputError`, a `ContractError` subclass.
A missing or malformed baseline produces `REJECT`; a malformed approval produces
`ESCALATE` after the hard checks pass. Callers must catch input errors as
evaluation failures and make no write. If the live
resource version is missing, the Guard raises an error; it cannot fabricate the
nonempty version required by the existing output contract.

## Rollback resource constraints

Policies may optionally include `resource_constraints`. The separate
[resource-policy example](../data/sentinel/samples/policy-resource-limits.json) adds these
rules and explicit state/dependency freshness limits without changing the starter
fixtures. It is a development example; review the ceilings for each real workload.

```json
{
  "resource_constraints": {
    "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
    "max_container_limits": {"cpu": "500m", "memory": "128Mi"},
    "require_requests_and_limits": true
  }
}
```

The Guard checks every regular and init container in the candidate rollback
template. Requests must not exceed corresponding limits. Configured ceilings
apply per container; they do not represent available cluster capacity or a total
Deployment budget across replicas.

A configured limit ceiling requires that resource's explicit limit. A configured
request ceiling requires its request or, when omitted, an effective request
derived from the provided limit. Unknown values cannot pass a configured ceiling.
`require_requests_and_limits: true` additionally requires explicit CPU and memory
values in both groups, even for resources without a configured ceiling. With the
flag false (the default), unconstrained resources may be omitted. Entirely omitting
`resource_constraints` preserves the starter's previous behaviour.

Quantities use exact arithmetic: CPU supports cores, `m` and exponent notation;
memory supports bytes, decimal SI, binary SI and exponent notation. Strings and
finite JSON numbers are accepted. Quantity tokens are limited to 256 characters.
Negative values, CPU finer than one milliCPU,
fractional bytes, malformed values and magnitudes beyond signed 64-bit quantities
are rejected. Fractional memory is rejected rather than rounded. Unknown keys
inside the constraint object or ceiling maps are policy errors. Admission
controllers, namespace defaults, quotas and scheduling can impose additional
rules; these checks do not replace API validation or prove recovery health.

Resource violations are hard rejections, including when the exact proposal has
operator approval. Update `policy_version` whenever these rules change.

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

`expires_at` is optional only for development profiles; live approvals require
expiry and a policy-bounded lifetime. Timestamps must be aware UTC. Approval cannot be dated
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
Coordinator now provides local SQLite approval persistence and routing through
`runtime/guard_pipeline.py`. Operator authentication, bounded live approvals,
revocation and distributed approval delivery remain integration work.

## Integration responsibilities

Use the [integration contract](guard-integration-contract.md) for the concrete
handoffs, unavailable-state behaviour, proposed revocation semantics and pending
team acceptance checklist.

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
state or provide distributed coordination. `valid_until` is clipped to decision
TTL, state/dependency freshness, approval expiry, baseline maximum age and
preview freshness. Preview freshness defaults to 10 seconds when a preview is
supplied without an explicit age limit. Evidence exactly at its age boundary can
produce a zero-validity decision, which the consumer helper refuses.

The file adapters, read-only live-state/reference metadata collection and
non-persisting admission preview are implemented in the
[runner](guard-runtime.md). Connected trusted baseline/approval stores, agreed
approval revocation and transport, integration with Coordinator/Executor/dependency
producers, resource checks for additional actions, and deployed service transport
remain future work. Admission dry-run is available and can be required by policy;
it is distinct from an optional running canary/recovery preview. The Guard is one component
of the recovery loop, not a complete autonomous recovery service.

## Demonstrate the execution handoff without teammate services

```text
python scripts/demo_guard_handoff.py
python scripts/demo_guard_handoff.py --scenario state-changed-after-recheck
```

This executable example connects the real Guard to a fake consumer and fake
conditional-write API. Its only successful case restores the exact checked
template. Rejected, escalated or malformed inputs never call the write API.
The output counts `write_attempts` separately from successfully applied `writes`.
A fresh
validation catches changed state, expired or withdrawn approval, and newly blocked
dependencies. A resource change after the final validation still causes an API
conflict with one attempt and zero applied writes. It uses fabricated development
approvals and has no Kubernetes/network access.

The example is an integration contract for Member 5, not a production Executor.
Persistent action records, idempotency, per-target coordination, crash
reconciliation and actual Kubernetes conditional writes remain Member 5's work.
Approval withdrawal is simulated by a fresh lookup returning no record; Member 1
must define authoritative revocation semantics and authenticate operators. Member
6 still supplies actual dependency evidence and post-action health verification.

## Reason codes

`REJECT` takes precedence over `ESCALATE`; rejected results contain only their
hard rejection reasons. An allowed result contains `VALIDATION_PASSED`.

| Check | Reason codes |
| --- | --- |
| Policy | `TARGET_NOT_ALLOWED`, `ACTION_NOT_ALLOWED`, `ACTION_NOT_IMPLEMENTED` |
| Live identity/version | `LIVE_TARGET_MISMATCH`, `TARGET_UID_MISMATCH`, `STALE_RESOURCE_VERSION` |
| Live timestamps | `LIVE_STATE_FROM_FUTURE`, `STALE_LIVE_STATE` |
| Baseline | `BASELINE_MISSING`, `INVALID_BASELINE`, `BASELINE_TARGET_MISMATCH`, `BASELINE_ID_MISMATCH`, `BASELINE_HASH_MISMATCH`, `BASELINE_FROM_FUTURE` |
| Rollback resources | `INVALID_RESOURCE_TEMPLATE`, `INVALID_RESOURCE_QUANTITY`, `RESOURCE_VALUES_REQUIRED`, `RESOURCE_REQUEST_EXCEEDS_LIMIT`, `RESOURCE_REQUEST_EXCEEDS_POLICY`, `RESOURCE_LIMIT_EXCEEDS_POLICY` |
| Dependency linkage/blockers | `DEPENDENCY_RESULT_MISMATCH`, `DEPENDENCY_BLOCKED` |
| Unknown dependency information | `DEPENDENCY_UNKNOWN`, `DEPENDENCY_RESULT_FROM_FUTURE`, `STALE_DEPENDENCY_RESULT` |
| Approval requiring review | `OPERATOR_APPROVAL_REQUIRED`, `INVALID_APPROVAL`, `APPROVAL_MISMATCH`, `APPROVAL_FROM_FUTURE`, `APPROVAL_EXPIRED` |
| Current matching operator denial | `OPERATOR_REJECTED` |

Version-2 reports also explain `BASELINE_HEALTH_*`, `BASELINE_AGE`,
`BASELINE_REFERENCE_*`, `BASELINE_IMAGE_DIGEST`, `SECURITY_*`, `WORKLOAD_*`,
`APPROVAL_LIFETIME` and `SERVER_DRY_RUN_*` checks. Inspect the report's exact path
and actual/expected metadata instead of relying only on a broad reason code.
