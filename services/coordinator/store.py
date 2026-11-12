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

LOG = log.get_logger("coordinator")


class CoordinatorError(ValueError):
    """An event conflicts with stored identity or the incident lifecycle."""


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

    def planning_blocker(self, incident_id):
        """Serialize recoveries for a workload, leaving other targets independent.

        Arrival order is durable (SQLite rowid). Include the physical workload
        name even across UID changes; Guard still checks the exact bound UID.
        """
        current = self.db.execute("SELECT rowid,* FROM incidents WHERE incident_id=?", (incident_id,)).fetchone()
        if current is None:
            raise CoordinatorError("Incident must be recorded first")
        target = json.loads(current["target"])
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
        """Return the newest accepted immutable finding for this action."""
        findings = (json.loads(row[0]) for row in self.db.execute("SELECT payload FROM messages"))
        return max((item for item in findings
                    if item["kind"] == "dependency_result" and item["action_id"] == action_id),
                   key=lambda item: _time(item["checked_at"]), default=None)

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
        self.discard_after_commit()
        try:
            yield
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

    def add_message(self, event_key, payload):
        self.db.execute("INSERT INTO messages VALUES(?,?)", (event_key, payload))

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
