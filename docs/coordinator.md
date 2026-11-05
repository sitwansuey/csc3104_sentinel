# Incident coordinator

Owner 1's first implementation uses Python's standard-library SQLite. It stores
incidents, immutable incoming messages, proposals, approval history and a durable
outbox. Lifecycle changes and generated commands commit in the same transaction.
`BEGIN IMMEDIATE` serializes competing local writers; SQLite is not coordination
across hosts. Use a separate store connection per worker.

`services/coordinator.py` owns lifecycle and routing decisions;
`services/coordinator_store.py` owns the existing SQLite schema and queries.
`SQLiteStore` and `CoordinatorError` remain importable from `services.coordinator`.
Workers use `pending_routes`, `route`, report storage and `transaction()` without
embedding Coordinator-table SQL. The transaction context still uses
`BEGIN IMMEDIATE`; result persistence and request acknowledgment share the same
commit. No schema migration or ORM is introduced.

## Run locally

From the repository root, with Python 3.12+:

```bash
python scripts/demo_coordinator.py
python -m unittest discover -s tests -p "test_*.py" -v
python scripts/check_contracts.py
```

The offline demo connects the real Observer and Guard to the coordinator using
`LocalEventBus`. Its planner, executor and verifier emit synthetic results. It
uses a temporary SQLite database, a fixed clock and no Kubernetes writes. It
demonstrates the route through approval and verification, not live recovery.

For a persistent local coordinator, the CLI accepts one contract-valid JSON
message at a time. The database defaults to `data/sentinel/runs/coordinator.sqlite3` relative
to the working directory. `data/sentinel/runs/` is already ignored by Git.

```bash
python scripts/coordinator.py ingest data/sentinel/samples/messages/incident.json
python scripts/coordinator.py ingest data/sentinel/samples/messages/proposal.json
python scripts/coordinator.py ingest data/sentinel/samples/messages/guard-result.json
python scripts/coordinator.py approve ACT-DEMO-001 local-operator
python scripts/coordinator.py status INC-DEMO-001
python scripts/coordinator.py pending
```

Use `--db PATH` before the subcommand to select a different database. CLI output
is JSON, with routing envelopes emitted one per line. The `approve` command also
prints the saved approval record. Fixture timestamps are synthetic; obtain a new
Guard result after approval rather than replaying the old escalation. Operator
IDs identify local callers; this prototype does not authenticate operators.

For strict live policies, add `--expires-at` with a future UTC timestamp. The
Python interface accepts the same optional `expires_at` keyword. Approval expiry
is persisted with the proposal/policy binding and checked by the Guard; its
maximum lifetime comes from policy. Omitting expiry retains development-mode
behaviour. See the [live Executor guide](executor-live.md) for the runtime handoff.

## Lifecycle and message ownership

| Input | Allowed prior state | Resulting state / routing |
| --- | --- | --- |
| Incident | New incident ID | OPEN; planner.request |
| Proposal | OPEN, REJECTED or FAILED | VALIDATING; guard.request |
| Dependency result | VALIDATING or AWAITING_APPROVAL | State unchanged; guard.request with dependency evidence |
| Guard ALLOW | VALIDATING, AWAITING_APPROVAL or READY | READY; executor.request |
| Guard ESCALATE | Same | AWAITING_APPROVAL; retain reason codes |
| Guard REJECT | Same | REJECTED; no execution command |
| Operator approval | AWAITING_APPROVAL with an approval-only reason | VALIDATING; guard.request with saved approval |
| Execution APPLIED or NOOP | READY or NEEDS_RECONCILIATION | VERIFYING; verifier.request |
| Execution FAILED or CONFLICT | Same | FAILED |
| Execution NEEDS_RECONCILIATION | Same | NEEDS_RECONCILIATION; executor.reconcile |
| Verified outcome | VERIFYING | RECOVERED, FAILED or NEEDS_REVIEW; incident.outcome |

Each result must match the full incident target, incident ID and active action.
One incident has one active action. A rejected or failed attempt can be replaced
with a new action ID; the old attempt and approval history remain in SQLite.
Uncertain writes block new proposals until the executor supplies a reconciliation
result. UNKNOWN health needs operator investigation; this first milestone has no
manual resolution transition. A recovered incident cannot be reopened.

Execution requires a stored ALLOW and a result timestamp at or after validation.
Accepted API writes never mark recovery by themselves. Outcomes require a health
window starting at or after execution; RECOVERED cannot contain failed checks.
The verifier owns duration, sample gaps and actual health measurements.

## Approval and validation

`approve(action_id, operator_id, store, expires_at=...)` records the SHA-256 of the complete
canonical proposal, the Guard's policy version, operator ID and UTC approval
time. Its format is accepted by the existing Guard. The canonical JSON encoding
matches the Guard fingerprint without importing its business logic.

Only OPERATOR_APPROVAL_REQUIRED, APPROVAL_EXPIRED and APPROVAL_MISMATCH
escalations are eligible. A changed policy version requires renewed approval
bound to that version, followed by another Guard evaluation.
Dependency, stale-state and policy problems require correction and validation.
Approval never sets READY; a newer Guard ALLOW must follow. Approval retries by
the same operator while revalidation is pending reuse the existing record.
Hard rejection cannot be reversed for that action ID. Policy changes require
fresh Guard validation and matching approval. The executor must reread state
and obtain fresh Guard validation under its own execution coordination.

## Local routing and restart recovery

Public Python interfaces live in `services/coordinator/core.py`:

```python
with SQLiteStore("data/sentinel/runs/coordinator.sqlite3") as store:
    bus = LocalEventBus()
    def consume(event):
        # Process event["payload"] using the service adapter.
        # Deduplicate event_id, and preserve stable action IDs across retries.
        acknowledge(store, event["event_id"])
    bus.subscribe("planner.request", consume)
    handle(incident_message, store, bus)
    dispatch_pending(store, bus)
    bus.drain()
```

Envelopes contain `event_id`, `topic` and `payload`. They are local adapter
commands, not additions to shared wire contracts. Payload keys are `incident`
for planner.request; `proposal` plus available `dependencies`/`approval` for
guard.request; `proposal`, `guard_result`, `approval` for executor.request;
`execution` for verifier.request; `proposal`, `execution` for executor.reconcile;
and `outcome` for incident.outcome. The receiving adapter obtains other required
inputs from trusted baseline/policy/live-state stores. See the demo for callbacks.

Publishing only queues an envelope. Consumers call `acknowledge` after successful
processing; CLI consumers can use `python scripts/coordinator.py ack EVENT_ID`.
Without acknowledgment, `pending` or `dispatch_pending` replays the same event ID
after restart. A publish failure leaves the committed message and outbox intact.
A crash after processing but before acknowledgment can repeat delivery: consumers
must be idempotent. Event IDs are scoped to the database. Drain outside database
transactions. An unknown topic remains queued; register its subscriber before
draining so it does not block later commands. All subscribers to a topic must
succeed before acknowledging its event.

Dispatch drops obsolete commands, including pending execution after a later
Guard rejection; LocalEventBus removes their queued copies too. This is not a
substitute for checking authorization immediately before a real cluster write.

Incident/proposal IDs are immutable. Result keys use kind, action ID and the
result's checked_at, recorded_at or window_end timestamp. Exact retries do not
change lifecycle state; changing a payload under the same key raises
CoordinatorError. New Guard results must be newer than the previous decision.

The shared Docker/kind lab remains operator-run (`bash scripts/lab.sh up`,
`health`, `fault`, `inspect`, `recover`). This milestone supplies a local runtime;
separate-process messaging, service deployments and live automated recovery
remain later integration work for the team.

## Recovery Guard worker

The runtime adapter in `runtime/guard_pipeline.py` consumes persisted
`guard.request` envelopes and calls the real Recovery Guard. It reads the stored
proposal and latest approval, reloads input data, and commits the Guard decision,
downstream command and consumed request acknowledgment in one SQLite transaction.
Input failures leave the request pending. Restarting the worker does not repeat
an already committed decision. It processes Guard requests independently of
unfinished planner or executor subscribers.

The CLI can process all currently pending requests once:

```powershell
python scripts/coordinator.py guard --policy data/sentinel/runs/policy.json --baseline data/sentinel/runs/baseline.json --state data/sentinel/runs/state.json --dependencies data/sentinel/runs/dependency-result.json
```

These paths are caller-supplied inputs, not files created by the command. `--state`
expects a single observation object, such as one entry in `data/sentinel/samples/observations.json`.
The baseline must have the format described in `docs/recovery-guard.md`; the lab's
revision-only rollback record is not a Guard baseline. The policy and baseline
must match the actual target identity. Do not treat synthetic sample UIDs or
dependency results as live evidence.

For current Kubernetes state, replace `--state` with an explicit context and
cluster ID (only a read-only Deployment GET is performed):

```powershell
python scripts/coordinator.py guard --policy data/sentinel/runs/policy.json --baseline data/sentinel/runs/baseline.json --context kind-sentinel-lab --cluster-id sentinel-lab
```

Without `--dependencies`, the worker uses the latest dependency result already
ingested for the action. If none exists, it supplies UNKNOWN and the Guard follows
the policy's escalation/rejection rule. Missing dependency verification never
implicitly authorizes execution. Missing baseline data causes Guard rejection.

After the first approval-only escalation, run `approve ACTION_ID OPERATOR_ID`,
then rerun `guard` to re-read state and evaluate the recorded approval. A policy
version change requires renewed approval. An explicit hard rejection remains
terminal for that action ID. `--now UTC_TIMESTAMP` is available only with recorded
state for reproducible fixture checks; live reads always use the current clock.

For a complete fixture-driven example with all required inputs wired in:

```powershell
python scripts/demo_coordinator.py
python -m unittest discover -s tests -p "test_guard*.py" -v
```

The demo now uses the same worker as the CLI. Its planner, executor and verifier
remain synthetic. This is local runtime integration; separate-process broker
delivery, production authentication and deployed automated recovery remain later
work. The worker holds a single-host SQLite write transaction during bounded input
reads; it is not intended as a distributed service runtime.
