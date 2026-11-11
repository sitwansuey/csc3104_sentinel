"""Version 1 worker delivery protocol backed by the Coordinator outbox."""
from contextlib import contextmanager
import json
import math
import time
from uuid import uuid4

from services.coordinator.core import LocalEventBus, acknowledge, dispatch_pending, record_message
from shared.canonical import sha256_hex as digest
from shared.contracts import validate_message
from runtime.service_identity import ProtocolError


TOPICS = {"planner": {"planner.request": "proposal"},
          "guard": {"guard.request": "guard_result"},
          "executor": {"executor.request": "execution_result", "executor.reconcile": "execution_result"},
          "verifier": {"verifier.request": "outcome", "dependency.request": "dependency_result"}}
INGRESS = {"observer": "incident", "verifier": "dependency_result"}


class Delivery:
    def __init__(self, store, *, clock=time.time, lease_seconds=30, retention_seconds=86400,
                 dependency_requests=False):
        if any(not math.isfinite(value) or value <= 0 for value in (lease_seconds, retention_seconds)):
            raise ValueError("Lease and retention must be positive")
        self.store, self.clock = store, clock
        self.lease, self.retention = lease_seconds, retention_seconds
        self.dependency_requests = dependency_requests
        store.db.execute("""CREATE TABLE IF NOT EXISTS transport_receipts (
            event_id INTEGER PRIMARY KEY REFERENCES outbox(id),
            delivery_id TEXT UNIQUE NOT NULL, owner TEXT, lease_token TEXT,
            attempts INTEGER NOT NULL DEFAULT 0, expires REAL NOT NULL DEFAULT 0,
            available REAL NOT NULL DEFAULT 0, finished REAL, result_digest TEXT)""")
        if dependency_requests:
            store.db.execute("""CREATE TABLE IF NOT EXISTS transport_dependencies (
                guard_event_id INTEGER PRIMARY KEY REFERENCES outbox(id),
                dependency_event_id INTEGER NOT NULL REFERENCES outbox(id))""")

    @contextmanager
    def transaction(self):
        db = self.store.db
        db.execute("BEGIN IMMEDIATE")
        try:
            now = self.clock()
            # Apply the Coordinator's existing revocation rules before exposing work.
            dispatch_pending(self.store, LocalEventBus())
            if self.dependency_requests:
                for request in self.store.pending_routes("guard.request"):
                    payload = json.loads(request["payload"])
                    if (payload.get("dependencies") is None and not db.execute(
                            "SELECT 1 FROM transport_dependencies WHERE guard_event_id=?", (request["id"],)).fetchone()):
                        self.store.enqueue("dependency.request", {"proposal": payload["proposal"],
                            "guard_event_id": request["id"]}, request["action_id"])
                        dependency_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
                        db.execute("INSERT INTO transport_dependencies VALUES(?,?)", (request["id"], dependency_id))
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
            candidates = db.execute(f"""SELECT o.* FROM outbox o LEFT JOIN transport_receipts r ON r.event_id=o.id
                WHERE o.delivered=0 AND o.topic IN ({placeholders})
                AND (o.topic!='guard.request' OR NOT EXISTS (
                    SELECT 1 FROM outbox dep WHERE dep.topic='dependency.request'
                    AND dep.action_id=o.action_id AND dep.delivered=0))
                AND (r.event_id IS NULL OR r.available <= ?) ORDER BY o.id""",
                (*topics, now)).fetchall()
            row = next((candidate for candidate in candidates if candidate["topic"] != "planner.request"
                        or self.store.planning_blocker(json.loads(candidate["payload"])["incident"]["incident_id"]) is None), None)
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
            payload = json.loads(row["payload"])
            if row["action_id"] and self.store.has_managed_approval(row["action_id"]) and "approval" in payload:
                # Protected approvals are read through the authenticated,
                # target-scoped approval service, not generic worker routing.
                payload["approval"] = None
            return {"delivery": {"delivery_id": receipt["delivery_id"], "lease_token": token,
                    "expires_at": expires, "attempt": receipt["attempts"] + 1, "topic": row["topic"],
                    "payload": payload}}

    def dependencies(self, role, proposal):
        if role not in {"guard", "executor"}:
            raise ProtocolError(403, "Role cannot retrieve authorization evidence")
        validate_message(proposal)
        if proposal["kind"] != "proposal":
            raise ProtocolError(400, "Expected the exact proposal")
        with self.transaction():
            action = self.store.action(proposal["action_id"])
            incident = self.store.incident(proposal["incident_id"])
            if (not action or not incident or action["proposal"] != proposal
                    or incident["active_action_id"] != proposal["action_id"]):
                raise ProtocolError(409, "Evidence does not match the active proposal")
            return {"dependencies": self.store.dependency_result(proposal["action_id"])}

    def renew(self, identity, role, delivery_id, token):
        """Extend live work only; retired work and replaced owners stay fenced."""
        with self.transaction() as (db, now):
            row = db.execute("""SELECT r.*,o.topic FROM transport_receipts r
                JOIN outbox o ON o.id=r.event_id WHERE delivery_id=?""", (delivery_id,)).fetchone()
            if row is None:
                raise ProtocolError(404, "Unknown or expired delivery")
            if row["owner"] != identity or row["topic"] not in TOPICS.get(role, {}):
                raise ProtocolError(403, "Delivery belongs to another worker")
            if token != row["lease_token"] or row["finished"] is not None or now >= row["expires"]:
                raise ProtocolError(409, "Delivery retired or lease expired/replaced")
            expires = now + self.lease
            delay = min(60, 2 ** min(row["attempts"] - 1, 6))
            db.execute("UPDATE transport_receipts SET expires=?,available=? WHERE event_id=?",
                       (expires, expires + delay, row["event_id"]))
            return {"expires_at": expires}

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
            source = payload["execution"] if row["topic"] == "verifier.request" else next(
                payload[key] for key in ("incident", "proposal", "execution") if key in payload)
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
                previous = self.store.guard_report(digest(result))
                if previous and previous != report:
                    raise ProtocolError(409, "Guard report conflicts with stored evidence")
                self.store.save_guard_report(source["action_id"], report)
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
