"""Version 1 worker delivery protocol backed by the Coordinator outbox."""
from contextlib import contextmanager
import hashlib
import json
import time
from uuid import uuid4

from services.coordinator.core import LocalEventBus, acknowledge, dispatch_pending, record_message
from shared.contracts import validate_message


TOPICS = {"planner": {"planner.request": "proposal"},
          "guard": {"guard.request": "guard_result"},
          "executor": {"executor.request": "execution_result", "executor.reconcile": "execution_result"},
          "verifier": {"verifier.request": "outcome"}}
INGRESS = {"observer": "incident", "verifier": "dependency_result"}


class ProtocolError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


class Delivery:
    def __init__(self, store, *, clock=time.time, lease_seconds=30, retention_seconds=86400):
        if min(lease_seconds, retention_seconds) <= 0:
            raise ValueError("Lease and retention must be positive")
        self.store, self.clock = store, clock
        self.lease, self.retention = lease_seconds, retention_seconds
        store.db.execute("""CREATE TABLE IF NOT EXISTS transport_receipts (
            event_id INTEGER PRIMARY KEY REFERENCES outbox(id),
            delivery_id TEXT UNIQUE NOT NULL, owner TEXT, lease_token TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL DEFAULT 0,
            available REAL NOT NULL DEFAULT 0, finished REAL, result_digest TEXT)""")

    @contextmanager
    def transaction(self):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock()
            # Apply the Coordinator's existing revocation rules before exposing work.
            dispatch_pending(self.store, LocalEventBus())
            db.execute("""UPDATE transport_receipts SET finished=?
                WHERE finished IS NULL AND event_id IN (SELECT id FROM outbox WHERE delivered=1)""", (now,))
            db.execute("DELETE FROM transport_receipts WHERE finished < ?", (now - self.retention,))
            yield db, now
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    def claim(self, identity, role):
        topics = TOPICS.get(role)
        if not topics:
            raise ProtocolError(403, "Role cannot claim requests")
        with self.transaction() as (db, now):
            placeholders = ",".join("?" for _ in topics)
            row = db.execute(f"""SELECT o.* FROM outbox o LEFT JOIN transport_receipts r ON r.event_id=o.id
                WHERE o.delivered=0 AND o.topic IN ({placeholders})
                AND (r.event_id IS NULL OR r.available <= ?) ORDER BY o.id LIMIT 1""",
                (*topics, now)).fetchone()
            if row is None:
                return {"delivery": None}
            db.execute("INSERT OR IGNORE INTO transport_receipts(event_id,delivery_id) VALUES(?,?)",
                       (row["id"], uuid4().hex))
            receipt = db.execute("SELECT * FROM transport_receipts WHERE event_id=?", (row["id"],)).fetchone()
            token = uuid4().hex
            expires = now + self.lease
            delay = min(60, 2 ** min(receipt["attempts"], 6))
            db.execute("""UPDATE transport_receipts SET owner=?, lease_token=?, attempts=attempts+1,
                expires=?, available=? WHERE event_id=?""", (identity, token, expires, expires + delay, row["id"]))
            return {"delivery": {"delivery_id": receipt["delivery_id"], "lease_token": token,
                    "expires_at": expires, "attempt": receipt["attempts"] + 1, "topic": row["topic"],
                    "payload": json.loads(row["payload"])}}

    def finish(self, identity, role, delivery_id, token, result, *, retry=False, report=None):
        with self.transaction() as (db, now):
            row = db.execute("""SELECT r.*, o.topic, o.payload FROM transport_receipts r
                JOIN outbox o ON o.id=r.event_id WHERE r.delivery_id=?""", (delivery_id,)).fetchone()
            if row is None:
                raise ProtocolError(404, "Unknown or expired delivery")
            if row["owner"] != identity or row["topic"] not in TOPICS.get(role, {}):
                raise ProtocolError(403, "Delivery belongs to another worker")
            if token != row["lease_token"]:
                raise ProtocolError(409, "Lease has been replaced")
            result_digest = digest(result) if report is None else digest({"result": result, "report": report})
            if row["finished"] is not None:
                if not retry and row["result_digest"] == result_digest:
                    return {"accepted": True, "replay": True}
                raise ProtocolError(409, "Delivery retired or result changed")
            if now >= row["expires"]:
                raise ProtocolError(409, "Lease expired")
            if retry:
                delay = min(60, 2 ** min(row["attempts"] - 1, 6))
                db.execute("UPDATE transport_receipts SET expires=?, available=? WHERE event_id=?",
                           (now, now + delay, row["event_id"]))
                return {"retry_after": delay}
            validate_message(result)
            payload = json.loads(row["payload"])
            source = next(payload[key] for key in ("incident", "proposal", "execution") if key in payload)
            if (result["kind"] != TOPICS[role][row["topic"]] or
                    any(result.get(key) != source[key] for key in ("incident_id", "target", "action_id") if key in source)):
                raise ProtocolError(409, "Result does not correspond to claimed request")
            if role == "guard":
                # Guard decisions and their exact evidence report are one
                # durable handoff. This is required after dependency revocation.
                latest = self.store.dependency_result(source["action_id"])
                if (not isinstance(report, dict)
                        or report.get("decision_sha256") != digest(result)
                        or report.get("proposal_sha256") != digest(source)
                        or report.get("inputs_sha256") != result.get("inputs_sha256")
                        or not isinstance(report.get("input_fingerprints"), dict)
                        or latest is not None and report["input_fingerprints"].get("dependencies") != digest(latest)):
                    raise ProtocolError(409, "Guard result needs its report bound to current evidence")
                previous = db.execute("SELECT report FROM guard_reports WHERE decision_sha256=?",
                                      (digest(result),)).fetchone()
                if previous and json.loads(previous[0]) != report:
                    raise ProtocolError(409, "Guard report conflicts with stored evidence")
                db.execute("INSERT OR IGNORE INTO guard_reports(decision_sha256,action_id,report) VALUES(?,?,?)",
                           (digest(result), source["action_id"], json.dumps(report, allow_nan=False)))
            elif report is not None:
                raise ProtocolError(403, "Only the Guard may submit an authorization report")
            record_message(result, self.store)
            acknowledge(self.store, row["event_id"])
            db.execute("UPDATE transport_receipts SET finished=?, result_digest=? WHERE event_id=?",
                       (now, result_digest, row["event_id"]))
            return {"accepted": True, "replay": False}

    def ingest(self, role, message):
        validate_message(message)
        if message["kind"] != INGRESS.get(role):
            raise ProtocolError(403, "Role cannot submit this message")
        with self.transaction():
            changed = record_message(message, self.store)
        return {"accepted": True, "replay": not changed}
