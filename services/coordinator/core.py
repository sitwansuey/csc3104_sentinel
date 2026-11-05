"""SQLite lifecycle and durable at-least-once local routing.

Routing envelopes are local commands, not shared wire messages or permission to
write Kubernetes state. The executor must still obtain fresh Guard validation.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

from shared.contracts import validate_message


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


class LocalEventBus:
    """Queued envelopes. Consumers acknowledge durable event IDs after success.

    Drain outside coordinator transactions. A failing subscriber leaves its
    envelope queued; earlier successful subscribers may see it again on retry.
    """

    def __init__(self):
        self.events = []
        self._handlers = {}

    def subscribe(self, topic, handler):
        self._handlers.setdefault(topic, []).append(handler)

    def publish(self, event):
        if not any(item["event_id"] == event["event_id"] for item in self.events):
            self.events.append(deepcopy(event))

    def cancel(self, event_id):
        self.events[:] = [event for event in self.events if event["event_id"] != event_id]

    def drain(self):
        while self.events:
            event = self.events[0]
            handlers = self._handlers.get(event["topic"], [])
            if not handlers:
                return
            for handler in handlers:
                handler(deepcopy(event))
            self.cancel(event["event_id"])


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

    def _enqueue(self, topic, payload, action_id=None):
        self.db.execute("INSERT INTO outbox(topic,payload,action_id) VALUES(?,?,?)",
                        (topic, _json(payload), action_id))

    def _state(self, incident_id, action_id, state):
        self.db.execute("UPDATE incidents SET state=? WHERE incident_id=?", (state, incident_id))
        self.db.execute("UPDATE actions SET state=? WHERE action_id=?", (state, action_id))


def _event_key(message):
    identity = message.get("action_id", message["incident_id"])
    # Result retries preserve their timestamps; each observation is immutable.
    stamp = next((message[key] for key in ("checked_at", "recorded_at", "window_end")
                  if key in message), "")
    return _json([message["kind"], identity, stamp])


def _apply(message, store):
    db = store.db
    kind, incident_id = message["kind"], message["incident_id"]
    if kind == "incident":
        db.execute("INSERT INTO incidents VALUES(?,?,?,NULL)",
                   (incident_id, _json(message["target"]), "OPEN"))
        store._enqueue("planner.request", {"incident": message})
        return
    incident = store.incident(incident_id)
    _require(incident is not None, "Incident must be recorded first")
    _require(incident["target"] == message["target"], "Incident target mismatch")
    action_id = message["action_id"]
    if kind == "proposal":
        _require(incident["state"] in {"OPEN", "REJECTED", "FAILED"},
                 "Only one active action is permitted; recovered incidents cannot reopen")
        _require(store.action(action_id) is None, "Action ID already belongs to a proposal")
        db.execute("INSERT INTO actions(action_id,incident_id,proposal,state) VALUES(?,?,?,?)",
                   (action_id, incident_id, _json(message), "VALIDATING"))
        db.execute("UPDATE incidents SET active_action_id=?,state='VALIDATING' WHERE incident_id=?",
                   (action_id, incident_id))
        store._enqueue("guard.request", {"proposal": message}, action_id)
        return
    action = store.action(action_id)
    _require(action is not None and action["incident_id"] == incident_id
             and incident["active_action_id"] == action_id, "Result does not match active action")
    state = action["state"]
    if kind == "dependency_result":
        _require(state in {"VALIDATING", "AWAITING_APPROVAL", "READY"},
                 "Dependency result arrived out of order")
        previous_finding = store.dependency_result(action_id)
        if previous_finding:
            _require(_time(message["checked_at"]) > _time(previous_finding["checked_at"]),
                "Dependency result must be newer than stored evidence")
        if state == "READY":
            # Revoke queued execution before accepting changed evidence. The
            # executor also rechecks current lifecycle before a conditional write.
            store._state(incident_id, action_id, "VALIDATING")
        if state == "READY" or previous_finding or action["guard_result"] or db.execute(
                "SELECT 1 FROM dependency_revalidations WHERE action_id=?", (action_id,)).fetchone():
            # Keep this requirement after escalation/approval too: a delayed
            # decision must not restore READY using the superseded evidence.
            db.execute("INSERT OR REPLACE INTO dependency_revalidations VALUES(?,?)",
                       (action_id, _digest(message)))
        store._enqueue("guard.request", {"proposal": action["proposal"], "dependencies": message,
                                       "approval": store.approval(action_id)}, action_id)
    elif kind == "guard_result":
        _require(state in {"VALIDATING", "AWAITING_APPROVAL", "READY"},
                 "Guard result arrived after action completion or rejection")
        previous = action["guard_result"]
        if previous:
            _require(_time(message["checked_at"]) > _time(previous["checked_at"]),
                     "Guard result must be newer than the stored decision")
        if message["verdict"] == "ALLOW":
            _require(message["validated_resource_version"] == action["proposal"]["based_on_resource_version"],
                     "Guard allowed a different resource version")
            latest = store.dependency_result(action_id)
            if latest:
                _require(_time(message["checked_at"]) >= _time(latest["checked_at"]),
                         "Guard ALLOW predates the latest dependency evidence")
            if message["schema_version"] == "2.0":
                _require(message["proposal_sha256"] == _digest(action["proposal"]),
                         "Guard allowed a different proposal")
            required = db.execute("SELECT dependency_sha256 FROM dependency_revalidations WHERE action_id=?",
                                  (action_id,)).fetchone()
            if required:
                row = db.execute("SELECT report FROM guard_reports WHERE decision_sha256=? AND action_id=?",
                                 (_digest(message), action_id)).fetchone()
                report = json.loads(row[0]) if row else {}
                _require(message["schema_version"] == "2.0"
                         and isinstance(report, dict)
                         and report.get("decision_sha256") == _digest(message)
                         and report.get("proposal_sha256") == message["proposal_sha256"]
                         and report.get("inputs_sha256") == message["inputs_sha256"]
                         and isinstance(report.get("input_fingerprints"), dict)
                         and report["input_fingerprints"].get("dependencies") == required[0],
                         "Dependency revalidation requires a Guard report bound to the latest evidence")
        next_state = {"ALLOW": "READY", "REJECT": "REJECTED", "ESCALATE": "AWAITING_APPROVAL"}[message["verdict"]]
        db.execute("UPDATE actions SET guard_result=? WHERE action_id=?", (_json(message), action_id))
        store._state(incident_id, action_id, next_state)
        if next_state == "READY":
            store._enqueue("executor.request", {"proposal": action["proposal"], "guard_result": message,
                                              "approval": store.approval(action_id)}, action_id)
    elif kind == "execution_result":
        _require(state in {"READY", "NEEDS_RECONCILIATION"}, "Execution requires an allowed active action")
        guard = action["guard_result"]
        _require(guard and guard["verdict"] == "ALLOW", "Execution requires Guard ALLOW")
        _require(_time(message["recorded_at"]) >= _time(guard["checked_at"]),
                 "Execution predates Guard validation")
        if action["execution_result"]:
            _require(_time(message["recorded_at"]) > _time(action["execution_result"]["recorded_at"]),
                     "Execution result is older than the stored result")
        status = message["status"]
        next_state = {"APPLIED": "VERIFYING", "NOOP": "VERIFYING", "FAILED": "FAILED",
                      "CONFLICT": "FAILED", "NEEDS_RECONCILIATION": "NEEDS_RECONCILIATION"}[status]
        if next_state == "VERIFYING":
            _require(message["resource_version"] is not None, "Successful execution needs a resource version")
        db.execute("UPDATE actions SET execution_result=? WHERE action_id=?", (_json(message), action_id))
        store._state(incident_id, action_id, next_state)
        if next_state == "VERIFYING":
            store._enqueue("verifier.request", {"execution": message}, action_id)
        elif next_state == "NEEDS_RECONCILIATION":
            store._enqueue("executor.reconcile", {"proposal": action["proposal"], "execution": message}, action_id)
    elif kind == "outcome":
        _require(state == "VERIFYING", "Outcome requires successful execution first")
        _require(_time(message["window_start"]) >= _time(action["execution_result"]["recorded_at"]),
                 "Health window predates execution")
        recovered = message["status"] == "RECOVERED"
        _require(not recovered or all(check["passed"] for check in message["checks"]),
                 "Recovered outcome cannot contain failed health checks")
        next_state = {"RECOVERED": "RECOVERED", "NOT_RECOVERED": "FAILED", "UNKNOWN": "NEEDS_REVIEW"}[message["status"]]
        store._state(incident_id, action_id, next_state)
        store._enqueue("incident.outcome", {"outcome": message}, action_id)


def record_message(message: dict, store: SQLiteStore) -> bool:
    """Record within an existing transaction, without committing or publishing.

    Runtime adapters use this to commit a result and its input acknowledgment
    together. Most callers should use handle instead.
    """
    _require(store.db.in_transaction, "record_message requires an active transaction")
    validate_message(message)
    payload, key = _json(message), _event_key(message)
    old = store.db.execute("SELECT payload FROM messages WHERE event_key=?", (key,)).fetchone()
    if old:
        _require(old[0] == payload, "Stable message ID reused with changed payload")
        return False
    _apply(message, store)
    store.db.execute("INSERT INTO messages VALUES(?,?)", (key, payload))
    return True


def handle(message: dict, store: SQLiteStore, event_bus) -> bool:
    """Atomically record and route. Return False for an exact retry.

    Publication failure propagates after commit. Retry handle or dispatch_pending
    after restart to replay commands from the durable outbox.
    """
    store.db.execute("BEGIN IMMEDIATE")
    try:
        changed = record_message(message, store)
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    dispatch_pending(store, event_bus)
    return changed


def approve(action_id: str, operator_id: str, store: SQLiteStore, *, expires_at: str | None = None) -> dict:
    """Persist approval and request fresh Guard evaluation; never grant execution."""
    _require(isinstance(operator_id, str) and bool(operator_id.strip()), "Operator identity is required")
    store.db.execute("BEGIN IMMEDIATE")
    try:
        action = store.action(action_id)
        previous = store.approval(action_id)
        if (action and action["state"] == "VALIDATING" and previous
                and previous["operator_id"] == operator_id
                and previous["proposal_sha256"] == _digest(action["proposal"])
                and previous["policy_version"] == action["guard_result"]["policy_version"]
                and previous.get("expires_at") == expires_at):
            store.db.execute("COMMIT")
            return previous
        _require(action is not None and action["state"] == "AWAITING_APPROVAL",
                 "Only an escalated active proposal can receive approval")
        guard = action["guard_result"]
        _require(set(guard["reason_codes"]) <= {"OPERATOR_APPROVAL_REQUIRED", "APPROVAL_EXPIRED", "APPROVAL_MISMATCH"},
                 "Approval cannot override state, dependency or policy escalation")
        now = store.now()
        _require(_time(now) >= _time(guard["checked_at"]), "Approval predates the Guard decision")
        if expires_at is not None:
            try:
                valid_expiry = isinstance(expires_at, str) and _time(expires_at) > _time(now)
            except (ValueError, TypeError, AttributeError) as exc:
                raise CoordinatorError("Approval expiry must be a future UTC timestamp") from exc
            _require(valid_expiry, "Approval expiry must be a future UTC timestamp")
        record = {"action_id": action_id, "operator_id": operator_id,
                  "proposal_sha256": _digest(action["proposal"]),
                  "policy_version": guard["policy_version"], "decision": "APPROVED", "approved_at": now}
        if expires_at is not None:
            record["expires_at"] = expires_at
        store.db.execute("INSERT INTO approvals(action_id,record) VALUES(?,?)", (action_id, _json(record)))
        store._state(action["incident_id"], action_id, "VALIDATING")
        store._enqueue("guard.request", {"proposal": action["proposal"], "approval": record}, action_id)
        store.db.execute("COMMIT")
    except BaseException:
        store.db.execute("ROLLBACK")
        raise
    return record


def dispatch_pending(store: SQLiteStore, event_bus) -> int:
    """Replay pending envelopes; acknowledge only after consumer success.

    publish only queues a command. A crash can repeat a delivered event, so
    consumers must deduplicate event_id and action_id. Call before bus.drain().
    """
    count = 0
    for row in store.db.execute("SELECT * FROM outbox WHERE delivered=0 ORDER BY id").fetchall():
        obsolete = False
        if row["action_id"]:
            action = store.action(row["action_id"])
            payload = json.loads(row["payload"])
            eligible = {"guard.request": {"VALIDATING", "AWAITING_APPROVAL"},
                        "executor.request": {"READY"}, "verifier.request": {"VERIFYING"},
                        "executor.reconcile": {"NEEDS_RECONCILIATION"}}
            obsolete = (row["topic"] in eligible and action["state"] not in eligible[row["topic"]])
            if row["topic"] == "executor.request":
                obsolete = obsolete or action["guard_result"] != payload["guard_result"]
        elif row["topic"] == "planner.request":
            incident = store.incident(json.loads(row["payload"])["incident"]["incident_id"])
            obsolete = incident["state"] != "OPEN"
        if obsolete:
            acknowledge(store, row["id"])
            if hasattr(event_bus, "cancel"):
                event_bus.cancel(row["id"])
            continue
        event_bus.publish({"event_id": row["id"], "topic": row["topic"], "payload": json.loads(row["payload"])})
        count += 1
    return count


def acknowledge(store: SQLiteStore, event_id: int) -> None:
    """Call only after successfully handling a routing envelope."""
    store.db.execute("UPDATE outbox SET delivered=1 WHERE id=?", (event_id,))
