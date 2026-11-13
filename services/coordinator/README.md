# Role 1 — Coordinator

`core.py` owns incident/proposal/approval/result lifecycle decisions and durable
local routing. `store.py` owns the SQLite schema, record queries, outbox and
transaction context. Schema changes are numbered migrations in `MIGRATIONS`,
tracked with `PRAGMA user_version` and applied in order when a store opens, so
older databases keep working. Every state change also appends a `transitions`
row in the same transaction ([observability](../../docs/observability.md)). `read.py` is the bounded public read-only projection used
by the dashboard. Existing public imports are re-exported by `__init__.py`.
`db.py` is the Database port every Coordinator statement goes through: SQLite
is the default and the only backend that runs, and it receives the same SQL
as before the port. Postgres is a SQL dialect only; its adapter refuses to
open until the owners decide on a Postgres driver (P5.01, see the
[distributed model](../../docs/distributed-model.md#the-coordinators-database-port-p501-stage-81-offline-only)).

Incoming messages have stable identities. An exact retry is ignored; changed
content under a stable identity is rejected. Lifecycle state and generated
commands commit together under BEGIN IMMEDIATE. Consumers record a result and
acknowledge its request atomically, then publish pending outbox entries. A
process crash can replay a command, so consumers still deduplicate it.

The normal path is OPEN → VALIDATING → AWAITING_APPROVAL → fresh validation →
READY → VERIFYING → RECOVERED. Failures/rejections/review states are explicit.
APPLIED alone creates a Verifier request. A changed dependency assessment
revokes queued execution and requires a bound current Guard report.

Approval binds the exact proposal and policy version; it only requests new
Guard evaluation. It cannot override hard constraints. The harness supplies
trusted demo approval. The optional [authenticated approval authority](../../docs/approvals.md)
adds target-scoped operator access, bounded expiry, durable revocation and current
Guard/Executor lookup without changing shared wire contracts.

`python -m runtime.coordinator_server --help` starts the combined delivery and
approval process. Its remote composition queues Verifier dependency work before
Guard turns, fences stale leases and accepts component results atomically.
The [remote worker guide](../../docs/remote-workers.md) covers owned component
journals, network ingress and the complete key-free HTTP recovery regression.

`python scripts/coordinator.py --help` exposes local ingestion/status/approval
and worker commands. The source uses one SQLite connection per worker and
separate execution/verification ledgers. This is single-host durability, not a
distributed message broker or Kubernetes fencing mechanism.

See [architecture](../../docs/architecture.md), [operation](../../docs/run.md)
and [optional HTTP delivery](../../runtime/README.md). Relevant regressions:
`test_coordinator`, `test_coordinator_expiry`, `test_coordinator_read`,
`test_dependency_revalidation`, `test_recovery_runner` and worker integration tests.
