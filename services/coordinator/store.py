"""Coordinator SQLite persistence; lifecycle decisions live in coordinator.py.

Mutations participate in the caller's transaction. They do not publish events,
authorize execution, or start Kubernetes operations. The existing schema and
public SQLiteStore import remain compatible with stored runs and team workers.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3


class CoordinatorError(ValueError):
    """An event conflicts with stored identity or the incident lifecycle."""


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


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
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY, target TEXT NOT NULL,
                state TEXT NOT NULL, active_action_id TEXT
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY, incident_id TEXT NOT NULL
                    REFERENCES incidents(incident_id),
                proposal TEXT NOT NULL, state TEXT NOT NULL,
                guard_result TEXT, execution_result TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                event_key TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY, action_id TEXT NOT NULL
                    REFERENCES actions(action_id), record TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
                action_id TEXT, delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS guard_reports (
                decision_sha256 TEXT PRIMARY KEY, action_id TEXT NOT NULL
                    REFERENCES actions(action_id), report TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dependency_revalidations (
                action_id TEXT PRIMARY KEY REFERENCES actions(action_id),
                dependency_sha256 TEXT NOT NULL
            );
        """)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

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

    def approval(self, action_id):
        row = self.db.execute("SELECT record FROM approvals WHERE action_id=? ORDER BY id DESC LIMIT 1",
                              (action_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def dependency_result(self, action_id):
        """Return the newest accepted immutable finding for this action."""
        findings = (json.loads(row[0]) for row in self.db.execute("SELECT payload FROM messages"))
        return max((item for item in findings
                    if item["kind"] == "dependency_result" and item["action_id"] == action_id),
                   key=lambda item: _time(item["checked_at"]), default=None)

    def enqueue(self, topic, payload, action_id=None):
        self.db.execute("INSERT INTO outbox(topic,payload,action_id) VALUES(?,?,?)",
                        (topic, _json(payload), action_id))

    def set_state(self, incident_id, action_id, state):
        self.db.execute("UPDATE incidents SET state=? WHERE incident_id=?", (state, incident_id))
        self.db.execute("UPDATE actions SET state=? WHERE action_id=?", (state, action_id))

    @property
    def in_transaction(self):
        return self.db.in_transaction

    @contextmanager
    def transaction(self):
        """Reserve the local writer; commit results and acknowledgments together."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def add_incident(self, incident_id, target, state):
        self.db.execute("INSERT INTO incidents VALUES(?,?,?,NULL)",
                        (incident_id, _json(target), state))

    def add_action(self, action_id, incident_id, proposal, state):
        self.db.execute("INSERT INTO actions(action_id,incident_id,proposal,state) VALUES(?,?,?,?)",
                        (action_id, incident_id, _json(proposal), state))

    def activate_action(self, incident_id, action_id, state):
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

    def add_approval(self, action_id, record):
        self.db.execute("INSERT INTO approvals(action_id,record) VALUES(?,?)", (action_id, _json(record)))

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
