"""Coordinator SQLite persistence; lifecycle decisions live in coordinator.py.

Mutations participate in the caller's transaction. They do not publish events,
authorize execution, or start Kubernetes operations. The existing schema and
public SQLiteStore import remain compatible with stored runs and team workers.
Schema changes are numbered migrations tracked by PRAGMA user_version, applied
in order when a store opens. Every lifecycle state change also appends a row to
the transitions journal in the same transaction.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3

# _json and _digest stay importable for approvals.py, core.py and approval_http.py.
from shared.canonical import canonical_json as _json, sha256_hex as _digest

from shared import log, tracing
# The Executor's own workload key, so the Coordinator's ownership row and the Executor's lock agree.
from services.executor.store import target_key

LOG = log.get_logger("coordinator")


class CoordinatorError(ValueError):
    """An event conflicts with stored identity or the incident lifecycle."""


class SupersededResult(CoordinatorError):
    """A worker result answers a request that is no longer the current one."""


# Append only: never edit a released migration; add the next number instead.
# Version 1 is the schema used before versioning. IF NOT EXISTS lets it adopt
# databases created by that code, which all report user_version 0.
MIGRATIONS = (
    (1, (
        """CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY, target TEXT NOT NULL,
            state TEXT NOT NULL, active_action_id TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS actions (
            action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL
                REFERENCES incidents(incident_id),
            proposal TEXT NOT NULL, state TEXT NOT NULL,
            guard_result TEXT, execution_result TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS messages (
            event_key TEXT PRIMARY KEY, payload TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS approvals (
            id INTEGER PRIMARY KEY, action_id TEXT NOT NULL
                REFERENCES actions(action_id), record TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS managed_approvals (
            approval_id INTEGER PRIMARY KEY REFERENCES approvals(id)
        )""",
        """CREATE TABLE IF NOT EXISTS approval_revocations (
            approval_id INTEGER PRIMARY KEY REFERENCES approvals(id),
            record TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
            action_id TEXT, delivered INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS guard_reports (
            decision_sha256 TEXT PRIMARY KEY, action_id TEXT NOT NULL
                REFERENCES actions(action_id), report TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS dependency_revalidations (
            action_id TEXT PRIMARY KEY REFERENCES actions(action_id),
            dependency_sha256 TEXT NOT NULL
        )""",
    )),
    (2, (
        # Append-only lifecycle journal. cause is the applied message's event
        # key, or 'approval'/'revocation'. outbox_high_water is the newest
        # outbox id when the change was made.
        """CREATE TABLE IF NOT EXISTS transitions (
            seq INTEGER PRIMARY KEY, incident_id TEXT NOT NULL, action_id TEXT,
            from_state TEXT, to_state TEXT NOT NULL, cause TEXT,
            outbox_high_water INTEGER, recorded_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS transitions_incident ON transitions(incident_id, seq)",
    )),
    (3, (
        # W3C traceparent of the span that enqueued a command, and of the span
        # that recorded an incident. Observability only; NULL on older rows.
        "ALTER TABLE outbox ADD COLUMN trace_context TEXT",
        "ALTER TABLE incidents ADD COLUMN trace_context TEXT",
    )),
    (4, (
        # Ordering by Coordinator sequence (P3.03). seq numbers accepted
        # messages in acceptance order; rows written before this version get
        # their rowid, which was already their acceptance order. answers is
        # the outbox id of the request a worker result answers (NULL for
        # ingress and for single-clock handle() calls). high_water is
        # max(outbox.id) just before the message was applied. producer is the
        # authenticated identity that delivered it, when there was one.
        "ALTER TABLE messages ADD COLUMN seq INTEGER",
        "ALTER TABLE messages ADD COLUMN answers INTEGER",
        "ALTER TABLE messages ADD COLUMN high_water INTEGER",
        "ALTER TABLE messages ADD COLUMN producer TEXT",
        "UPDATE messages SET seq=rowid WHERE seq IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS messages_seq ON messages(seq)",
        # The seq of the newest message accepted for the incident.
        "ALTER TABLE incidents ADD COLUMN high_water INTEGER",
        # Each worker's latest clock estimate against the Coordinator's clock
        # (Cristian's algorithm, measured by the worker): offset is
        # Coordinator time minus worker time, uncertainty is half the
        # round-trip time. reported_at uses the Coordinator's clock.
        """CREATE TABLE IF NOT EXISTS worker_clocks (
            identity TEXT PRIMARY KEY, offset REAL NOT NULL, uncertainty REAL NOT NULL,
            max_abs_offset REAL NOT NULL, reports INTEGER NOT NULL, reported_at REAL NOT NULL
        )""",
    )),
    (5, (
        # Restore detection (P3.06). generation is a random id made once per
        # database file and kept for its life, so a database recreated or
        # swapped for another file has a different one. commit_counter grows
        # by one in every transaction that changes a row, so a copy of this
        # file restored from an earlier backup has a lower counter. Every
        # worker reply carries both (runtime/worker_http.py).
        """CREATE TABLE IF NOT EXISTS store_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1), generation TEXT NOT NULL,
            commit_counter INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        )""",
        """INSERT OR IGNORE INTO store_meta(id,generation,commit_counter,created_at)
            VALUES(1, lower(hex(randomblob(16))), 0, strftime('%Y-%m-%dT%H:%M:%fZ','now'))""",
        # The kind and action of each accepted message, indexed, so the newest
        # message of a kind for an action is found without reading every row.
        "ALTER TABLE messages ADD COLUMN kind TEXT",
        "ALTER TABLE messages ADD COLUMN action_id TEXT",
        """UPDATE messages SET kind=json_extract(payload,'$.kind'),
            action_id=json_extract(payload,'$.action_id') WHERE json_valid(payload)""",
        "CREATE INDEX IF NOT EXISTS messages_kind_action ON messages(kind, action_id)",
    )),
    (6, (
        # Target ownership for Executor work (P3.04). target_key is
        # services/executor/store.py::target_key (cluster, namespace, kind and
        # name; no UID, so a recreated Deployment keeps its row); uid is kept
        # beside it. epoch grows by one on every claim of an executor.* command
        # for the target and never goes back. holder, delivery_id and event_id
        # name the latest claim. seal_required blocks every other Executor
        # command and new planning for the target until an executor.seal for
        # seal_action_id reports SEALED; sealed_epoch is the epoch of that seal.
        """CREATE TABLE IF NOT EXISTS target_ownership (
            target_key TEXT PRIMARY KEY, uid TEXT, epoch INTEGER NOT NULL DEFAULT 0,
            holder TEXT, delivery_id TEXT, event_id INTEGER,
            seal_required INTEGER NOT NULL DEFAULT 0, seal_action_id TEXT,
            sealed_epoch INTEGER NOT NULL DEFAULT 0
        )""",
    )),
)
SCHEMA_VERSION = MIGRATIONS[-1][0]


def schema_version(db):
    return db.execute("PRAGMA user_version").fetchone()[0]


def migrate(db, migrations=MIGRATIONS):
    """Bring a database up to the newest version, applying missing steps in order.

    The steps and their version run in one BEGIN IMMEDIATE transaction, and the
    version is checked again inside it, so concurrent openers apply each step
    once. A failure rolls back the whole run and leaves the old version. A
    database newer than this code is refused.
    """
    latest = migrations[-1][0]
    if schema_version(db) == latest:
        return latest
    db.execute("BEGIN IMMEDIATE")
    try:
        current = schema_version(db)
        _require(current <= latest, "Coordinator database schema is newer than this code")
        for version, statements in migrations:
            if version <= current:
                continue
            for statement in statements:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {int(version)}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise
    return latest


def _time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() != timedelta(0):
        raise CoordinatorError("Expected a timezone-aware UTC timestamp")
    return parsed


def _require(condition, reason):
    if not condition:
        raise CoordinatorError(reason)


class SQLiteStore:
    """Single-host durable records. Use one connection per worker."""

    def __init__(self, path=":memory:", *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._after_commit = []
        self._cause = (None, None)
        try:
            migrate(self.db)
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self._after_commit = []
        self.db.close()

    def after_commit(self, callback):
        """Run callback once the current transaction commits; drop it on rollback.

        Callbacks only emit logs. They must not raise or write to the store.
        """
        if self.db.in_transaction:
            self._after_commit.append(callback)
        else:
            callback()

    def run_after_commit(self):
        callbacks, self._after_commit = self._after_commit, []
        for callback in callbacks:
            callback()

    def discard_after_commit(self):
        self._after_commit = []

    @contextmanager
    def transition_cause(self, cause, kind):
        """Label transitions written in this block, e.g. with a message event key."""
        previous, self._cause = self._cause, (cause, kind)
        try:
            yield
        finally:
            self._cause = previous

    def _journal(self, incident_id, action_id, to_state, cause=None):
        """Append the transition in the caller's transaction; log it after commit."""
        row = self.db.execute("SELECT state FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        from_state = row[0] if row else None
        cause, kind = (cause, cause) if cause is not None else self._cause
        high_water = self.db.execute("SELECT max(id) FROM outbox").fetchone()[0]
        seq = self.db.execute("""INSERT INTO transitions(incident_id,action_id,from_state,to_state,cause,
            outbox_high_water,recorded_at) VALUES(?,?,?,?,?,?,?)""",
            (incident_id, action_id, from_state, to_state, cause, high_water, self.now())).lastrowid
        # The line is written after commit, possibly outside this span: name it explicitly.
        span = tracing.current()
        trace = span or self.trace_context(incident_id=incident_id)
        ids = {"trace_id": trace and trace.trace_id, "span_id": span and span.span_id}
        self.after_commit(lambda: log.emit(
            LOG, "coordinator.state.transition", seq=seq, incident_id=incident_id, action_id=action_id,
            from_state=from_state, to_state=to_state, cause=kind, outbox_high_water=high_water, **ids))

    def transitions(self, incident_id=None):
        query, parameters = "SELECT * FROM transitions", ()
        if incident_id is not None:
            query, parameters = query + " WHERE incident_id=?", (incident_id,)
        return [dict(row) for row in self.db.execute(query + " ORDER BY seq", parameters)]

    def now(self):
        value = self.clock()
        _require(isinstance(value, datetime) and value.utcoffset() == timedelta(0),
                 "Store clock must return a UTC datetime")
        return value.isoformat().replace("+00:00", "Z")

    def incident(self, incident_id):
        row = self.db.execute("SELECT * FROM incidents WHERE incident_id=?",
                              (incident_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["target"] = json.loads(result["target"])
        return result

    def action(self, action_id):
        row = self.db.execute("SELECT * FROM actions WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("proposal", "guard_result", "execution_result"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def actions_for_incident(self, incident_id):
        """Every action proposed for the incident, oldest first."""
        rows = self.db.execute("SELECT action_id FROM actions WHERE incident_id=? ORDER BY rowid", (incident_id,))
        return [self.action(row[0]) for row in rows.fetchall()]

    def planning_blocker(self, incident_id):
        """Serialize recoveries for a workload, leaving other targets independent.

        Arrival order is durable (SQLite rowid). Include the physical workload
        name even across UID changes; Guard still checks the exact bound UID.
        A seal still required for the workload blocks every incident on it,
        this one included, until the seal reports (P3.04).
        """
        current = self.db.execute("SELECT rowid,* FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if current is None:
            raise CoordinatorError("Incident must be recorded first")
        target = json.loads(current["target"])
        owner = self.target_ownership(target)
        if owner is not None and owner["seal_required"]:
            sealed = self.action(owner["seal_action_id"])
            return sealed["incident_id"] if sealed else incident_id
        active = {"VALIDATING", "AWAITING_APPROVAL", "READY", "VERIFYING", "NEEDS_RECONCILIATION"}
        for row in self.db.execute("SELECT rowid,* FROM incidents ORDER BY rowid"):
            if row["incident_id"] == incident_id:
                continue
            other = json.loads(row["target"])
            if any(other[k] != target[k] for k in ("cluster_id", "namespace", "kind", "name")):
                continue
            if row["state"] in active or (row["state"] == "OPEN" and row["rowid"] < current["rowid"]):
                return row["incident_id"]
        return None

    def target_ownership(self, target):
        """The ownership row of a workload (migration 6), or None before its first Executor claim."""
        row = self.db.execute("SELECT * FROM target_ownership WHERE target_key=?", (target_key(target),)).fetchone()
        return dict(row) if row else None

    def claim_target(self, target, holder, delivery_id, event_id):
        """Record a new claim of an Executor command for the workload; return its new epoch.

        Every claim is a new lease, so the epoch grows by one each time, also
        when the same identity claims again: an earlier incarnation of that
        holder may still be inside its API call.
        """
        key = target_key(target)
        self.db.execute("""INSERT INTO target_ownership(target_key,uid,epoch,holder,delivery_id,event_id)
            VALUES(?,?,1,?,?,?) ON CONFLICT(target_key) DO UPDATE SET uid=excluded.uid, epoch=epoch+1,
            holder=excluded.holder, delivery_id=excluded.delivery_id, event_id=excluded.event_id""",
            (key, target["uid"], holder, delivery_id, event_id))
        return self.db.execute("SELECT epoch FROM target_ownership WHERE target_key=?", (key,)).fetchone()[0]

    def require_seal(self, action):
        """Block the action's workload until an executor.seal for it reports; enqueue that seal once.

        Return True when a seal was enqueued now. Call in the transaction that
        abandons an Executor command some holder may still be executing.
        """
        target, action_id = action["proposal"]["target"], action["action_id"]
        owner = self.target_ownership(target)
        if (owner is not None and owner["seal_required"] and owner["seal_action_id"] == action_id
                and self.pending_routes("executor.seal", action_id=action_id)):
            return False
        self.db.execute("""INSERT INTO target_ownership(target_key,uid,seal_required,seal_action_id)
            VALUES(?,?,1,?) ON CONFLICT(target_key) DO UPDATE SET seal_required=1,
            seal_action_id=excluded.seal_action_id""", (target_key(target), target["uid"], action_id))
        self.enqueue("executor.seal", {"proposal": action["proposal"]}, action_id)
        ids = {"incident_id": action["incident_id"], "action_id": action_id}
        self.after_commit(lambda: log.emit(LOG, "coordinator.seal.required", level=log.WARNING, **ids))
        return True

    def finish_seal(self, target, epoch, *, release):
        """Record a seal's answer at epoch; release unblocks the workload, otherwise it stays blocked."""
        self.db.execute("UPDATE target_ownership SET sealed_epoch=?, seal_required=? WHERE target_key=?",
                        (epoch, 0 if release else 1, target_key(target)))

    def approval(self, action_id):
        row = self.approval_row(action_id)
        if row is None or self.db.execute(
                "SELECT 1 FROM approval_revocations WHERE approval_id=?", (row["id"],)).fetchone():
            return None
        return json.loads(row["record"])

    def approval_row(self, action_id):
        return self.db.execute("SELECT * FROM approvals WHERE action_id=? ORDER BY id DESC LIMIT 1",
                               (action_id,)).fetchone()

    def has_managed_approval(self, action_id):
        return self.db.execute("""SELECT 1 FROM managed_approvals m JOIN approvals a ON a.id=m.approval_id
            WHERE a.action_id=? LIMIT 1""", (action_id,)).fetchone() is not None

    def current_approval(self, action_id, *, managed_only=False):
        """Read unrevoked, unexpired approval; expiry is exclusive."""
        if managed_only:
            row = self.approval_row(action_id)
            if row is None or not self.db.execute(
                    "SELECT 1 FROM managed_approvals WHERE approval_id=?", (row["id"],)).fetchone():
                return None
        record = self.approval(action_id)
        if record and record.get("expires_at") and _time(self.now()) >= _time(record["expires_at"]):
            return None
        return record

    def approval_history(self, action_id):
        return [{"approval_id": row["id"], "approval": json.loads(row["record"]),
                 "revocation": json.loads(row["revocation"]) if row["revocation"] else None}
                for row in self.db.execute("""SELECT a.id,a.record,r.record AS revocation FROM approvals a
                    LEFT JOIN approval_revocations r ON r.approval_id=a.id
                    WHERE a.action_id=? ORDER BY a.id""", (action_id,))]

    def dependency_result(self, action_id):
        """Return the most recently accepted immutable finding for this action.

        Acceptance order (seq), not the producer's timestamp. Single-clock
        callers only accept strictly newer findings, so for them the two agree.
        """
        meta = self.accepted_meta("dependency_result", action_id)
        return meta["message"] if meta else None

    def accepted_meta(self, kind, action_id):
        """The newest accepted message of kind for action_id with its ordering data, or None.

        Uses the messages(kind, action_id) index from migration 5.
        """
        row = self.db.execute("""SELECT payload,seq,answers,high_water,producer FROM messages
            WHERE kind=? AND action_id IS ? ORDER BY seq DESC LIMIT 1""", (kind, action_id)).fetchone()
        if row is None:
            return None
        return {"message": json.loads(row[0]), "seq": row[1], "answers": row[2],
                "high_water": row[3], "producer": row[4]}

    def position(self):
        """(generation, commit_counter) of this database: which file it is, and how far it has got."""
        row = self.db.execute("SELECT generation, commit_counter FROM store_meta WHERE id=1").fetchone()
        return (row[0], row[1]) if row else (None, None)

    def count_commit(self, changes_before):
        """Advance the commit counter if this transaction changed a row. Call just before COMMIT.

        changes_before is the connection's total_changes when the transaction
        began, so a transaction that only read leaves the counter alone.
        """
        if self.db.total_changes != changes_before:
            self.db.execute("UPDATE store_meta SET commit_counter=commit_counter+1 WHERE id=1")

    def outbox_high_water(self):
        return self.db.execute("SELECT coalesce(max(id),0) FROM outbox").fetchone()[0]

    def newest_request(self, action_id, *topics):
        """The id of the newest outbox request of topics for action_id, or None."""
        placeholders = ",".join("?" for _ in topics)
        return self.db.execute(f"SELECT max(id) FROM outbox WHERE action_id=? AND topic IN ({placeholders})",
                               (action_id, *topics)).fetchone()[0]

    def record_worker_clock(self, identity, offset, uncertainty, reported_at):
        """Keep a worker's latest clock estimate and the largest offset it has reported."""
        self.db.execute("""INSERT INTO worker_clocks(identity,offset,uncertainty,max_abs_offset,reports,reported_at)
            VALUES(?,?,?,abs(?),1,?) ON CONFLICT(identity) DO UPDATE SET offset=excluded.offset,
            uncertainty=excluded.uncertainty, max_abs_offset=max(max_abs_offset, excluded.max_abs_offset),
            reports=reports+1, reported_at=excluded.reported_at""",
            (identity, offset, uncertainty, offset, reported_at))

    def worker_clocks(self):
        return {row["identity"]: dict(row) for row in self.db.execute("SELECT * FROM worker_clocks ORDER BY identity")}

    def trace_context(self, *, incident_id=None, action_id=None):
        """The trace context stored with an incident, found by incident or action id.

        It labels log lines only and is never used to decide anything.
        """
        if incident_id is not None:
            row = self.db.execute("SELECT trace_context FROM incidents WHERE incident_id=?",
                                  (incident_id,)).fetchone()
        elif action_id is not None:
            row = self.db.execute("""SELECT i.trace_context FROM actions a
                JOIN incidents i ON i.incident_id=a.incident_id WHERE a.action_id=?""", (action_id,)).fetchone()
        else:
            row = None
        return tracing.parse_traceparent(row[0]) if row else None

    def enqueue(self, topic, payload, action_id=None):
        # Rows written outside a span (local pipelines, approvals) join the incident's trace.
        span = tracing.current()
        trace = span or self.trace_context(action_id=action_id)
        event_id = self.db.execute("INSERT INTO outbox(topic,payload,action_id,trace_context) VALUES(?,?,?,?)",
                                   (topic, _json(payload), action_id, trace and trace.traceparent)).lastrowid
        # Logged after commit, possibly in another span: name this row's trace explicitly.
        ids = {"trace_id": trace and trace.trace_id, "span_id": span and span.span_id}
        self.after_commit(lambda: log.emit(LOG, "coordinator.outbox.enqueued", event_id=event_id,
                                           topic=topic, action_id=action_id, **ids))

    def set_state(self, incident_id, action_id, state, *, cause=None):
        self._journal(incident_id, action_id, state, cause)
        self.db.execute("UPDATE incidents SET state=? WHERE incident_id=?", (state, incident_id))
        self.db.execute("UPDATE actions SET state=? WHERE action_id=?", (state, action_id))

    @property
    def in_transaction(self):
        return self.db.in_transaction

    @contextmanager
    def transaction(self):
        """Reserve the local writer; commit results and acknowledgments together."""
        self.db.execute("BEGIN IMMEDIATE")
        changes = self.db.total_changes
        self.discard_after_commit()
        try:
            yield
            self.count_commit(changes)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.discard_after_commit()
            raise
        self.run_after_commit()

    def add_incident(self, incident_id, target, state):
        self._journal(incident_id, None, state)
        self.db.execute("""INSERT INTO incidents(incident_id,target,state,active_action_id,trace_context)
            VALUES(?,?,?,NULL,?)""", (incident_id, _json(target), state, tracing.current_traceparent()))

    def add_action(self, action_id, incident_id, proposal, state):
        self.db.execute("INSERT INTO actions(action_id,incident_id,proposal,state) VALUES(?,?,?,?)",
                        (action_id, incident_id, _json(proposal), state))

    def activate_action(self, incident_id, action_id, state):
        self._journal(incident_id, action_id, state)
        self.db.execute("UPDATE incidents SET active_action_id=?,state=? WHERE incident_id=?",
                        (action_id, state, incident_id))

    def required_dependency_hash(self, action_id):
        row = self.db.execute("SELECT dependency_sha256 FROM dependency_revalidations WHERE action_id=?",
                              (action_id,)).fetchone()
        return row[0] if row else None

    def require_dependency_revalidation(self, action_id, dependency_hash):
        self.db.execute("INSERT OR REPLACE INTO dependency_revalidations VALUES(?,?)",
                        (action_id, dependency_hash))

    def guard_report(self, decision_hash, action_id=None):
        query = "SELECT report FROM guard_reports WHERE decision_sha256=?"
        parameters = [decision_hash]
        if action_id is not None:
            query += " AND action_id=?"
            parameters.append(action_id)
        row = self.db.execute(query, parameters).fetchone()
        return json.loads(row[0]) if row else None

    def save_guard_report(self, action_id, report):
        self.db.execute("INSERT OR IGNORE INTO guard_reports(decision_sha256,action_id,report) VALUES(?,?,?)",
                        (report["decision_sha256"], action_id, json.dumps(report, allow_nan=False)))

    def save_guard_result(self, action_id, result):
        self.db.execute("UPDATE actions SET guard_result=? WHERE action_id=?", (_json(result), action_id))

    def save_execution_result(self, action_id, result):
        self.db.execute("UPDATE actions SET execution_result=? WHERE action_id=?", (_json(result), action_id))

    def message_payload(self, event_key):
        row = self.db.execute("SELECT payload FROM messages WHERE event_key=?", (event_key,)).fetchone()
        return row[0] if row else None

    def add_message(self, event_key, payload, *, high_water, answers=None, producer=None, incident_id=None):
        """Store an accepted message with the next Coordinator sequence number; return it."""
        seq = self.db.execute("SELECT coalesce(max(seq),0)+1 FROM messages").fetchone()[0]
        message = json.loads(payload)
        self.db.execute("""INSERT INTO messages(event_key,payload,seq,answers,high_water,producer,kind,action_id)
            VALUES(?,?,?,?,?,?,?,?)""", (event_key, payload, seq, answers, high_water, producer,
                                         message.get("kind"), message.get("action_id")))
        if incident_id is not None:
            self.db.execute("UPDATE incidents SET high_water=? WHERE incident_id=?", (seq, incident_id))
        return seq

    def add_approval(self, action_id, record, *, managed=False):
        cursor = self.db.execute("INSERT INTO approvals(action_id,record) VALUES(?,?)", (action_id, _json(record)))
        if managed:
            self.db.execute("INSERT INTO managed_approvals VALUES(?)", (cursor.lastrowid,))

    def pending_routes(self, *topics, action_id=None):
        query = "SELECT * FROM outbox WHERE delivered=0"
        parameters = []
        if topics:
            query += " AND topic IN (" + ",".join("?" for _ in topics) + ")"
            parameters.extend(topics)
        if action_id is not None:
            query += " AND action_id=?"
            parameters.append(action_id)
        return self.db.execute(query + " ORDER BY id", parameters).fetchall()

    def route(self, event_id):
        return self.db.execute("SELECT * FROM outbox WHERE id=?", (event_id,)).fetchone()

    def acknowledge_route(self, event_id):
        self.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (event_id,))
