# Recovery Guard integration contract

**Status: proposed for team review.** The implemented behaviour below is tested;
the team acceptance checklist is pending. This document does not claim that other
members have accepted a transport, storage design or approval protocol.

The first integration supports one typed action, `ROLLBACK`, on a Deployment.
Current Guard results use `schema_version: "2.0"`; the other wire-message kinds
remain version 1. Shared validation can read historical version-1 Guard results,
but the execution-consumer helper refuses them. Messages are validated by
[shared/contracts.py](../shared/contracts.py). The
[runner guide](guard-runtime.md) describes commands and the
[Guard guide](recovery-guard.md) defines the input fields and reason codes. The
[version-2 guide](guard-v2.md) describes the stronger policy gates and reports;
the [Member 4 role guide](guard-role.md) explains the owner's concrete tasks.

## Owners and handoffs

| Owner | Provides or consumes | Required handoff |
| --- | --- | --- |
| Member 1: Coordinator and environment | Trusted policy, approval lookup and event routing; coordinates baseline-store ownership | Supply the reviewed policy version and current authoritative approval for the exact proposal. Select the baseline-store owner and persistence/transport. Keep operator authentication and hosted environment ownership here. |
| Member 2: Observer | Incident evidence and observed state | Supply actual cluster/namespace/name/UID, capture time and resource version. An incident snapshot is evidence for planning; Member 5 obtains fresh state before execution. |
| Member 3: Planner | `proposal` message | Supply the explicit baseline ID/hash, target identity, resource version, action ID, rationale and evidence references. Do not provide policy or approval as authoritative Planner output. |
| Member 4: Guard | Version-2 `guard_result`, explanation report or evaluation error | Validate trusted inputs, apply hard rejections before approval, and return reasons, checked version, exact proposal/input hashes and expiry. Perform no recovery write. |
| Member 5: Executor | Consumes fresh Guard validation | Coordinate per target, reread inputs, validate immediately before writing, and condition the write on the checked identity/version. Own persistent deduplication, bounded retries and crash reconciliation. |
| Member 6: Dependencies and Verifier | `dependency_result`, trusted baseline health evidence and post-action `outcome` | Supply linked dependency findings and target/template-bound stability-window evidence. Verify sustained health after the write; API acceptance alone is not recovery. The trusted producer/store remains to be implemented. |

Every target includes `cluster_id`, `namespace`, `kind`, `name` and `uid`.
Recreating the same named Deployment changes its identity. Resource versions are
passed unchanged and compared for equality.

## Phase 2 interface available now

The implemented development boundary is JSON files and the command line:

```text
python -m services.guard_cli --help
```

Required input flags are `--proposal`, `--policy`, `--baseline`,
`--dependencies`, and exactly one of `--live-state` or `--context`. An approval is
supplied through `--approval` when available. File formats are specified in the
Guard guide; approval and snapshot records remain local adapter formats rather
than new shared wire-message kinds.
Offline `--preview PATH` can supply a replayed admission record. `--report PATH`
writes a bounded private explanation atomically before emitting a decision;
its parent must exist and it cannot alias any input or kubeconfig. Reusing an
unrelated report path replaces that report, so persistence owners should use a
distinct path per validation.

Live mode additionally requires independently configured `--cluster-id` and
accepts a private `--kubeconfig`. Context-to-cluster mapping belongs to trusted
operator configuration. Live mode requires a strict reviewed `profile: "live"`
policy before any cluster request. The adapter reads the Deployment and, when
needed, referenced ConfigMap/Secret UID/resourceVersion using metadata-only
JSONPath output. RBAC still requires `get` on the underlying objects, including
Secrets; scope it to the selected namespace and resource names. Secret values do
not enter the Python collector or reports. `--server-dry-run` additionally needs
Deployment `patch` permission; all preview requests use `--dry-run=server` and
persist no recovery changes. `--now` is permitted only for offline fixture
replay; live reads and evaluation use the real UTC clock.

The result contains `proposal_sha256`, `inputs_sha256` and `valid_until`.
`assert_decision_usable(...)` requires an unexpired trusted version-2 `ALLOW`
bound to the exact proposal and complete inputs. Persist the decision/report
alongside those inputs; hashes establish binding, not authentication or a signed
execution credential. Member 5 must still obtain current trusted inputs and
fresh validation immediately before the conditional write. A cached result does
not reserve state, record revocation or provide distributed coordination.

| CLI result | Output | Consumer behaviour |
| --- | --- | --- |
| Exit `0`: `ALLOW` | One version-2 `guard_result` on stdout | Verify exact input binding and expiry, refresh trusted state/approval, revalidate under execution coordination and write conditionally. |
| Exit `2`: `REJECT` | One `guard_result` on stdout | Zero writes. Hard failures cannot be overridden by operator approval. |
| Exit `3`: `ESCALATE` | One `guard_result` on stdout | Zero writes. Obtain the missing information or review, then evaluate again. |
| Exit `1`: evaluation/input/adapter error | One `guard_error` on stderr; stdout empty | Zero writes. Treat the request as unevaluated; do not synthesize an `ALLOW` or missing resource version. |

Function callers may inject adapters directly into their own service boundary.
`evaluate(...)` raises `ContractError` or its `GuardInputError` subclass for
invalid required inputs. Callers must handle those errors as zero-write paths.

## Unavailable state, freshness and dependencies

| Condition | Implemented Guard behaviour |
| --- | --- |
| Live state unavailable, missing resource version, malformed policy or malformed wire message | Evaluation error; no valid authorization result. |
| Valid state with changed UID or resource version | `REJECT`. Reread and replan/revalidate as needed. |
| State timestamp in the future or older than configured state limit | `REJECT`. |
| Missing or invalid saved baseline object | `REJECT`. An unreadable required baseline file instead produces a runner input error. |
| Missing trusted health/configuration evidence | `ESCALATE`; malformed or mismatched evidence and changed referenced UID/version reject. |
| Unsafe rollback security, images or resource budgets | `REJECT`, regardless of matching approval. |
| Required admission preview unavailable/stale | `ESCALATE`; mismatched inputs or altered admitted template reject. |
| Dependency result absent/unreadable/malformed | Evaluation/input error. The dependency producer may explicitly report a valid `UNKNOWN` result when it has incomplete evidence. |
| Dependency blockers or mismatched incident/action/target | `REJECT`. |
| Dependency `UNKNOWN`, unresolved unknowns, future timestamp or over-age result | Follow policy: `ESCALATE` or `REJECT`. Operator approval cannot clear the uncertainty. |

Maximum state/dependency ages are optional only in development profiles. Omitting a limit
means there is no maximum age; it must not be interpreted as a hidden default.
The [development resource policy](../samples/policy-resource-limits.json) explicitly
uses 15 seconds for state and 30 seconds for dependencies. These values and its
synthetic identities require review before use with a real workload. Evidence
exactly at an age limit is accepted; an older reading is stale.
Strict live profiles require explicit freshness, bounded approval, baseline
health/age/digest/compatibility rules, security rules and workload budgets. The
version-2 guide lists the required keys. Decision expiry is clipped to the TTL,
state/dependency limits, approval expiry, baseline maximum age and preview
freshness (default 10 seconds when a preview has no explicit age limit). At an
evidence boundary, a development `ALLOW` may have zero validity and cannot pass the consumer
helper's strict `now < valid_until` check.

CPU/memory ceilings apply to every regular and init container in the candidate
rollback template. A configured ceiling requires a verifiable value; missing
capped values cannot bypass it. Per-container ceilings are not a quota,
schedulability check or total replica budget.
Separate workload constraints cover effective Pod requests and a conservative
replica/surge request bound. They do not reserve cluster capacity. A successful
Deployment dry-run validates admission acceptance of the exact template, not
future Pod admission, scheduling or sustained health.

## Approval and revocation handoff

Implemented approval records bind `action_id`, the entire proposal's SHA-256,
`policy_version`, an operator identity, `APPROVED`/`REJECTED`, and UTC approval
time. Expiry is optional only in development profiles; live approvals require
expiry and a bounded lifetime. A changed proposal or policy
needs a matching new approval; an expired or future record cannot authorize a
write. A valid matching `REJECTED` record vetoes execution even when approval is
otherwise optional. Malformed or mismatched records escalate after hard checks.

**Proposed live lookup semantics for Member 1 and Member 5 to confirm:**

1. Member 1 authenticates the operator, owns the approval store, and serves the
   current authoritative record. A Planner-supplied JSON record is not trusted.
2. Member 5 performs a fresh approval lookup during final validation. An
   unavailable lookup aborts execution rather than falling back to a cached
   approval. The current CLI expects its caller to perform that lookup before
   supplying the trusted approval file; it has no connected approval store. The
   separate local Coordinator worker reads the latest approval from SQLite.
3. For an approval-required policy, withdrawal can be represented by a successful
   lookup returning no active approval. That escalates. If approval is optional,
   absence is not a veto: an authoritative matching `REJECTED` record is needed
   to express an operator veto. Do not invent a new `REVOKED` decision field; the
   current format does not support it.
4. The two owners must choose approval lifetime, store failure behaviour,
   revocation cutoff and coordination with execution. The current offline
   demonstration covers withdrawal **before** the final lookup. It does not
   guarantee revocation that races with a write after that lookup.

Treat an action ID as immutable for retries. Proposed integration rule: changed
proposal content gets a new action ID, dependency inspection and approval.
Member 5's persistent ledger must refuse changed payloads reusing an existing
action ID. The Guard itself has no historical ledger; its dependency linkage
uses incident/action/target, so producer-side action immutability is important.

## Executable acceptance evidence

```text
python scripts/check_contracts.py
python -m unittest discover -s tests -p "test_*.py"
python scripts/demo_guard_handoff.py
python scripts/demo_guard_v2.py
```

The handoff demo exercises the real evaluator with a fake consumer/API: one
approved current rollback attempts one write and applies the exact checked
template; rejected, escalated and invalid inputs never attempt the write API.
It also covers changed state, expired or
withdrawn approval, newly blocked dependencies, and a conditional-write conflict
after the final `ALLOW` (one attempt, zero applied writes). Synthetic approvals
have no live authority.

These checks let the owners implement independently today. The local Coordinator
worker in `runtime/guard_pipeline.py` now persists Guard decisions and routes
approval-driven revalidation through SQLite; `scripts/demo_coordinator.py` and
`tests/test_guard_integration.py` exercise this connection. Its approval creation
does not yet supply live expiry, and its Kubernetes reader does not collect
reference metadata or admission previews. Full live-policy input collection and
audit-report persistence still need to be connected to that worker. Production
authentication, the real Executor and the dependency producer remain integration
work.
The version-2 demo exercises health, security, configuration, workload and
admission gates with fabricated evidence; it contacts no cluster. No shared
hosting environment was created by these Guard improvements. Planner, Executor
and Verifier remain stubs. The Observer and Coordinator have local
implementations; trusted baseline/health stores and distributed transport still
need integration.

## Team acceptance checklist

- [ ] Member 1 confirms trusted policy/baseline ownership, approval store and
  operator authentication, lifetime, revocation cutoff, transport and failure
  behaviour.
- [ ] Member 2 confirms snapshot identities, timestamps and freshness assumptions.
- [ ] Member 3 confirms immutable action IDs and exact baseline/proposal hashes.
- [ ] Member 4 confirms policy thresholds, baseline/security/workload constraints,
  decision lifetime, report fields, resource ceilings, reason codes and
  evaluation-error handling with the affected owners.
- [ ] Member 5 confirms fresh input lookups, version-2 exact input/result binding,
  expiry checks, admission-preview requirements,
  per-target coordination, conditional writes and persistent retry handling.
- [ ] Member 6 confirms dependency linkage, unknown-state semantics and freshness,
  trusted healthy-baseline evidence and independent sustained outcome verification.
- [ ] Members 1/4/5/6 choose the distributed transport and authenticated trust
  boundary before enabling automated live recovery.

Record reviewer names and accepted decisions in the integration PR. Leave these
boxes unchecked until the corresponding owners have actually reviewed them.
