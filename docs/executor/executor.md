# Restricted rollback executor

This guide explains the durable execution core: immutable action identities,
local workload coordination, conditional writes and crash reconciliation. The
[live Kubernetes guide](executor-live.md) covers execution credentials, strict
Guard inputs, deployment setup, measured live testing and cleanup.

Role 5 restores one exact saved healthy Deployment Pod template through an
injected API adapter. The live runtime uses the actual Kubernetes adapter;
offline regression tests use isolated test doubles. Both exercise the same
execution ledger and state machine. Planner and Verifier remain separate
responsibilities.

Run the offline regression tests from the repository root with Python 3.12+:

```bash
python -m unittest discover -s tests -p "test_executor*.py" -v
python scripts/cli/check_contracts.py
```

Test doubles live only in `tests/support/`. These tests use synthetic inputs to
reproduce concurrency, storage failures and uncertain API outcomes reliably.
The supported execution entrypoint and real-cluster experiment are in the live
guide. Offline test results do not replace live Kubernetes evidence.

## Execution and validation

The Executor validates the proposal contract and allows only `ROLLBACK` for a
Deployment. The proposal identifies a stored baseline and its Pod-template
SHA-256. The baseline resolver supplies the complete saved template; the
Executor checks identity and hash rather than asking Kubernetes to undo whichever
revision happens to be latest.

Under target coordination, the Executor reads current state and trusted policy,
baseline, dependency and approval records. It calls the real Guard and checks the
version-2 result's exact input binding and expiry before mutation. The earlier
Coordinator ALLOW is not reused as final execution authorization. Human approval
does not override a hard Guard rejection.

Approval is looked up once during that fresh validation. The Executor checks the
bound decision's expiry before journaling and again after the journal commits,
immediately before the API call. The runtime also checks that the Coordinator's
active action, lifecycle and stored Guard result still match at those points.
These checks do not repeat the approval-store lookup. Withdrawal after that
lookup, or a lifecycle change after the final check, can still race with the
write. A live revocation cutoff needs an agreed protocol; this increment does
not make approval storage and Kubernetes atomic.

The write preconditions use the target UID and the exact resource version that
the Guard checked. Resource versions are opaque strings. A stale proposal is
rejected through fresh validation, or a later race fails the conditional write.
The Executor never changes a proposal's resource version under the same action
ID. Replanning with changed intent requires a new action ID and new validation
and approval as required by policy.

The fake restore API models one atomic operation: test UID/resource version,
replace `spec.template` with the resolved baseline and write action evidence in
a Deployment metadata annotation. The evidence lives outside the Pod template,
so it does not alter the baseline's template hash. The template and evidence must
be committed together; separate writes cannot support this reconciliation rule.

## Idempotency and coordination

The Executor owns a SQLite ledger separate from the Coordinator database. Before
execution it binds the action ID to the canonical SHA-256 of the entire proposal.
Canonical JSON sorts object keys; array order and every proposal field remain
significant. This includes rationale and preconditions, not just the template.

- Repeating a completed action with the same proposal returns its saved result.
- Reusing an action ID with changed content raises `ActionConflict` and cannot
  perform a write.
- An interrupted action is inspected from its persisted stage and attempt
  evidence rather than treated as a new request.

Per-target nonblocking OS file locks coordinate local processes using the same
ledger and lock directory. A target's lock key contains cluster, namespace, kind
and name. It excludes UID so an object recreated under the same name still
contends for the same lock. `TargetBusy` means the caller should retry later.
The lock is held across fresh validation, attempt journaling, API access and
ledger completion; process termination releases the OS lock.

Releasing that OS lock does not clear an uncertain attempt. A persisted
`WRITE_STARTED` or `UNCERTAIN` row excludes other actions for the same workload,
including a recreated UID. The ledger checks this before admitting new work or
resuming an action that has not yet attempted a write. Only resolving the
uncertain journal can clear that exclusion.

SQLite supplies durable records, while the file lock protects the whole local
read/validate/write sequence. All cooperating workers must share these paths.
Separate ledgers or lock directories do not coordinate. This is single-host
coordination; it has no distributed lease, fencing token or ownership-loss
protocol.

## The write and ledger are separate commits

Before calling the API, the Executor commits `WRITE_STARTED`, the checked target
and resource version, the proposal/template hashes and a new attempt nonce to its
ledger. The API then writes the template and matching action evidence atomically.
Finally, the Executor saves the result. Persisting intent before the call makes
a restart aware that a write may have happened.

| Interruption point | Restart behaviour |
| --- | --- |
| Before a write attempt is journaled | Fresh validation can precede execution |
| After `WRITE_STARTED`, before the API call | No evidence proves whether the call happened; return NEEDS_RECONCILIATION without another mutation |
| API commits, then response is lost or process crashes | Read current state; matching action evidence and exact template can prove APPLIED |
| Result commits, then command acknowledgment is lost | Return the saved result on duplicate delivery |

Reconciliation requires the expected target UID, action ID, proposal fingerprint,
attempt nonce and actual Pod-template hash to match the journal. Merely seeing
the desired template is insufficient: another actor might have installed it, or
it might have existed before this attempt. A copied or stale marker with changed
template also does not prove success. A missing target, changed UID, overwritten
evidence, unavailable read or inconsistent marker remains
NEEDS_RECONCILIATION. Absence of evidence does not prove that an earlier write
failed and cannot authorize a retry.

This conservative rule also covers a crash immediately before the API call:
reconciliation cannot distinguish it from a call whose outcome is unknown.
Resolving an inconclusive attempt requires further authoritative evidence or an
operator workflow; this increment does not automatically clear it.

Each new reconciliation result is retained in the ledger history. If the clock
has not advanced beyond the previous result, its `recorded_at` advances by one
microsecond to preserve the Coordinator's strict result ordering. This is logical
ordering, not a latency or recovery-time measurement. Replaying a completed
result preserves its original timestamp and payload exactly.

Only explicitly retryable `ReadUnavailable` failures receive bounded read
retries. A confirmed conditional-write conflict is reported without retrying the
write. A timeout or other unknown write outcome triggers reconciliation. It
never starts another mutation to find out whether the first succeeded.

## Coordinator and Verifier integration

The runtime worker handles pending `executor.request` and `executor.reconcile`
commands from the actual Coordinator. It fetches the stored proposal and latest
approval, executes or reconciles through injected adapters, records an eligible
contract-valid result and acknowledges the command. Executor durability remains
in its own ledger; event/result replay bridges a crash before Coordinator
completion. No Coordinator transaction can make its database and a Kubernetes
write atomic.

The ledger also acts as a result outbox. Saving a result atomically marks it
undelivered, including a completed DONE result. The worker obtains these through
`undelivered_results()` and only calls `acknowledge_result(result)` after the
Coordinator result or delivery review has committed. Acknowledgment matches the
exact saved payload, so an older delivery cannot acknowledge a newer
reconciliation result. A crash between the two database commits repeats result
delivery, preserving both the original timestamp and the one-write guarantee.
Canceled routing commands cannot erase this delivery obligation.

An accepted APPLIED result moves the Coordinator to VERIFYING and emits
`verifier.request`. It does not mean RECOVERED. The Verifier owns sustained
service/dependency health, and the Coordinator owns the final incident state.
NEEDS_RECONCILIATION blocks ordinary replacement actions until the uncertain
write is resolved.

Every worker run also calls `reconcile_pending_executions(adapters)`, scanning
unfinished writes and undelivered completed results independently of Coordinator
command eligibility.
It uses the public `reconcile()` entrypoint, which cannot start a mutation. A
later Guard rejection or canceled command therefore does not prevent inspection
of evidence for a write that might already have committed. Busy targets stay
pending for another run.

A saved result may conflict with the Coordinator's newer Guard decision, active
action or terminal lifecycle state. The worker preserves the original result
and records a durable delivery review in the Coordinator database instead of
overriding that state or changing a completed result's timestamp. For an executed
command, acknowledgment follows result or review persistence. Inspect
`execution_delivery_reviews(store)` from `runtime.executor_pipeline` for the
result, reason, Coordinator state and Guard snapshot. Event ID zero identifies
reconciliation without a pending command. These records require manual lifecycle
review; this increment does not implement a resolution command or automatically
route verification from an incompatible result.

## Remaining gates

The fake API tests conditional writes and failure windows; it cannot establish
Kubernetes admission, RBAC or controller behaviour. The later
[live increment](executor-live.md) supplies the real adapter and
ServiceAccount/RBAC manifest. This offline demonstration does not deploy an
executor service. The original operator rollback scripts remain separate.

The live increment adds conditional patching, scoped permissions and an actual
kind experiment. Shared coordination with ownership fencing is still needed
before cross-host workers. Production baseline provenance, approval authentication and
revocation/write race semantics also require agreement with Roles 1 and 4.
Real dependency collection and sustained outcome measurement require Role 6.
