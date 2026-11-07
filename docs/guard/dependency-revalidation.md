# Dependency revalidation

The runtime consumes real Verifier assessments through `handle(dependency_result,
store, bus)`. The Guard worker reads the latest accepted finding for the active
action, alongside current state, baseline, policy and approval. It never treats
missing dependency evidence as CLEAR.

Run this deterministic demonstration from the repository root, with Python 3.12+
and no external packages or cluster:

```powershell
python scripts/demos/demo_dependency_revalidation.py
python -m unittest tests.guard.test_dependency_revalidation tests.guard.test_guard_integration -v
```

The demo uses the real Verifier graph evaluator, SQLite Coordinator and Guard,
with explicit synthetic infrastructure inputs. Each scenario first obtains
approval and READY, then submits changed evidence before any execution. Its
assertions fail the command if an obsolete execution remains eligible.

| New assessment | State immediately after ingestion | New Guard verdict | Final state |
| --- | --- | --- | --- |
| CLEAR | VALIDATING, execution cancelled | ALLOW after all other checks | READY |
| BLOCKED | VALIDATING, execution cancelled | REJECT: DEPENDENCY_BLOCKED | REJECTED |
| UNKNOWN | VALIDATING, execution cancelled | ESCALATE: DEPENDENCY_UNKNOWN | AWAITING_APPROVAL |

The UNKNOWN example uses the sample policy's ESCALATE setting. Setting
`unknown_dependency_result` to `REJECT` rejects instead. An approval cannot
override either dependency outcome. The tests cover both settings, stale/future
findings, mismatched identities, delayed deliveries, duplicates and restart.

A new finding for an authorized but unexecuted action moves READY back to
VALIDATING. Pending execution commands are cancelled. Guard must evaluate the
newest finding before another command can be issued. CLEAR still requires state,
baseline, resource and approval gates. Exact retries preserve the existing state;
older findings and changed payloads at the same timestamp are refused. Findings
must match the incident, active action and complete target identity. A finding
newer than stored evidence still revokes READY if its timestamp predates the last
Guard decision: later delivery does not mean the decision used that evidence.

A configured dependency reader can provide fresh assessments directly. The
worker compares its finding with accepted stored evidence, uses the newer one,
and fails closed on different payloads with an equal timestamp. A matching newer
reader finding is persisted atomically with the decision and report. Covered
Guard requests are acknowledged together, avoiding repeated evaluations of
superseded requests. Reader-only callers continue to work.

After accepted evidence changes, or a first finding arrives after an earlier
Guard decision, the additive local `dependency_revalidations` table retains the
newest required dependency fingerprint through approval and restart. This also
protects actions still in VALIDATING or AWAITING_APPROVAL from delayed decisions.
ALLOW requires a version-2 decision and persisted Guard report bound to that
finding. The worker rechecks the decision's full input fingerprint against its
actual inputs before recording it. Reports, lifecycle changes and request
acknowledgments share a transaction. A delayed decision using an old assessment
cannot restore READY simply by having a later `checked_at`. Direct consumers
should use `process_guard_requests` after evidence changes; a bare `handle(ALLOW)`
without a matching persisted report is refused. Legacy development messages
remain accepted before dependency revalidation is required.

After the Coordinator records an execution result, this API refuses dependency
updates. Execution retains its existing fresh Guard validation and conditional
write checks. A write already in flight can race revocation; this local
transaction does not provide a distributed fencing guarantee.
Dependency timestamps, readers and stored reports must come from trusted local
components. Hashes bind inputs; they do not authenticate a producer or protect
against a writer able to forge the SQLite audit records. Deployment, telemetry
collection and cross-host execution fencing remain outside this offline demo.
