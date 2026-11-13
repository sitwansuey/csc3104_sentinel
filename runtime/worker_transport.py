"""Version 1 worker delivery protocol backed by the Coordinator outbox."""
from contextlib import contextmanager
import json
import math
import time
from uuid import uuid4

from services.coordinator.core import MAX_STALE_REPLANS, LocalEventBus, acknowledge, dispatch_pending, record_message
from services.coordinator.store import SupersededResult
from shared.canonical import sha256_hex as digest
from shared.contracts import validate_message
from shared.tracing import parse_traceparent
from runtime.service_identity import ProtocolError


TOPICS = {"planner": {"planner.request": "proposal"},
          "guard": {"guard.request": "guard_result"},
          "executor": {"executor.request": "execution_result", "executor.reconcile": "execution_result",
                       "executor.seal": "fence_result"},
          "verifier": {"verifier.request": "outcome", "dependency.request": "dependency_result"}}
# Executor commands are claimed under per-target ownership (P3.04): one live claim per workload, and
# each claim gets the workload's next epoch, returned to the Executor as fence_epoch.
EXECUTOR_TOPICS = frozenset(TOPICS["executor"])
INGRESS = {"observer": "incident", "verifier": "dependency_result"}


def delivery_ids(delivery):
    """Correlation IDs of a delivery for log lines; never its lease token or payload."""
    if not isinstance(delivery, dict):
        return {}
    payload = delivery.get("payload") if isinstance(delivery.get("payload"), dict) else {}
    source = next((payload[key] for key in ("proposal", "execution", "incident")
                   if isinstance(payload.get(key), dict)), {})
    ids = {"delivery_id": delivery.get("delivery_id"), "event_id": delivery.get("event_id"),
           "topic": delivery.get("topic"), "attempt": delivery.get("attempt"),
           "action_id": source.get("action_id"), "incident_id": source.get("incident_id")}
    return {key: value for key, value in ids.items() if value is not None}


MAX_REPORTED_OFFSET = 10 ** 7  # Seconds; a larger reported clock estimate is malformed.


def parse_clock_estimate(value):
    """(offset, uncertainty) from a worker's "offset;uncertainty" report, or None when malformed."""
    if not isinstance(value, str) or value.count(";") != 1:
        return None
    try:
        offset, uncertainty = (float(part) for part in value.split(";"))
    except ValueError:
        return None
    if (not math.isfinite(offset) or not math.isfinite(uncertainty) or abs(offset) > MAX_REPORTED_OFFSET
            or not 0 <= uncertainty <= MAX_REPORTED_OFFSET):
        return None
    return offset, uncertainty


def within_skew_bound(estimate, bound):
    """True when the worst-case offset |offset| + uncertainty is at most the bound."""
    return estimate is not None and abs(estimate[0]) + estimate[1] <= bound


class Delivery:
    """max_clock_skew (seconds), when set, refuses claims from a worker whose
    latest reported clock estimate is missing or outside the bound.
    stale_replans, when above 0, lets a Guard refusal whose only reason is
    STALE_RESOURCE_VERSION, or an Executor CONFLICT that provably wrote
    nothing, queue up to that many re-plans per incident in all
    (services/coordinator/core.py::replan_after_stale and
    replan_after_conflict); 0 keeps both final."""

    def __init__(self, store, *, clock=time.time, lease_seconds=30, retention_seconds=86400,
                 dependency_requests=False, max_clock_skew=None, stale_replans=0):
        if any(not math.isfinite(value) or value <= 0 for value in (lease_seconds, retention_seconds)):
            raise ValueError("Lease and retention must be positive")
        if max_clock_skew is not None and (not math.isfinite(max_clock_skew) or max_clock_skew <= 0):
            raise ValueError("The clock skew bound must be positive and finite")
        if type(stale_replans) is not int or not 0 <= stale_replans <= MAX_STALE_REPLANS:
            raise ValueError(f"stale_replans must be an integer from 0 to {MAX_STALE_REPLANS}")
        self.store, self.clock = store, clock
        self.lease, self.retention = lease_seconds, retention_seconds
        self.dependency_requests = dependency_requests
        self.max_clock_skew = max_clock_skew
        self.stale_replans = stale_replans
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
        changes = db.total_changes
        self.store.discard_after_commit()
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
            self._seal_abandoned(db)
            db.execute("""UPDATE transport_receipts SET finished=?
                WHERE finished IS NULL AND event_id IN (SELECT id FROM outbox WHERE delivered=1)""", (now,))
            db.execute("DELETE FROM transport_receipts WHERE finished < ?", (now - self.retention,))
            yield db, now
            self.store.count_commit(changes)  # Restore detection: see SQLiteStore.position.
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            self.store.discard_after_commit()  # Rolled-back transitions are never logged.
            raise
        self.store.run_after_commit()

    def _seal_abandoned(self, db):
        """Require a seal for each executor.request withdrawn while claimed (P3.04).

        dispatch_pending has just acknowledged commands whose action changed
        (an approval revoked, new dependency evidence, a new Guard decision).
        A claimed, unfinished receipt for one means some Executor may still be
        inside its conditional write, whose lease check is check-then-act, so
        the workload is sealed before anything else runs on it. A command that
        was never claimed has no receipt and needs no seal.
        """
        for row in db.execute("""SELECT o.action_id FROM transport_receipts r JOIN outbox o ON o.id=r.event_id
                WHERE r.finished IS NULL AND r.attempts > 0 AND o.delivered=1
                AND o.topic='executor.request' ORDER BY o.id""").fetchall():
            action = self.store.action(row["action_id"])
            if action is not None:
                self.store.require_seal(action)

    def _target_free(self, db, now, candidate):
        """False while another Executor claim on the candidate's workload is live, or a seal is still due.

        Only the seal itself may be claimed while its workload waits for it:
        the abandoned holder may be paused or partitioned for any time, so the
        seal never waits for that holder's lease.
        """
        target = json.loads(candidate["payload"])["proposal"]["target"]
        owner = self.store.target_ownership(target)
        if owner is None:
            return True
        if owner["seal_required"] and candidate["topic"] != "executor.seal":
            return False
        if owner["event_id"] is None or owner["event_id"] == candidate["id"]:
            return True
        held = db.execute("SELECT finished, expires FROM transport_receipts WHERE event_id=?",
                          (owner["event_id"],)).fetchone()
        return held is None or held["finished"] is not None or held["expires"] <= now

    def _claimable(self, db, now, candidate):
        if candidate["topic"] == "planner.request":
            incident_id = json.loads(candidate["payload"])["incident"]["incident_id"]
            return self.store.planning_blocker(incident_id) is None
        if candidate["topic"] in EXECUTOR_TOPICS:
            return self._target_free(db, now, candidate)
        return True

    def _owner_epoch(self, row, identity):
        """The ownership epoch of this claim, or None when a later claim took the workload."""
        owner = self.store.target_ownership(json.loads(row["payload"])["proposal"]["target"])
        if owner is None or owner["event_id"] != row["event_id"] or owner["holder"] != identity:
            return None
        return owner["epoch"]

    def _record_clock(self, identity, estimate, now):
        if estimate is not None:
            self.store.record_worker_clock(identity, estimate[0], estimate[1], now)

    def report_clock(self, identity, estimate):
        """Store a worker's clock estimate; answer with nothing but the server time header."""
        with self.transaction() as (_db, now):
            self._record_clock(identity, estimate, now)
            return {"recorded": estimate is not None}

    def claim(self, identity, role, clock_estimate=None):
        topics = TOPICS.get(role)
        if not topics:
            raise ProtocolError(403, "Role cannot claim requests")
        with self.transaction() as (db, now):
            self._record_clock(identity, clock_estimate, now)
            if self.max_clock_skew is None or within_skew_bound(clock_estimate, self.max_clock_skew):
                return self._claim(db, now, identity, topics)
        # Committed above, so the refused worker's estimate stays on record.
        raise ProtocolError(409, "Worker clock skew exceeds the bound")

    def _claim(self, db, now, identity, topics):
        placeholders = ",".join("?" for _ in topics)
        candidates = db.execute(f"""SELECT o.* FROM outbox o LEFT JOIN transport_receipts r ON r.event_id=o.id
            WHERE o.delivered=0 AND o.topic IN ({placeholders})
            AND (o.topic!='guard.request' OR NOT EXISTS (
                SELECT 1 FROM outbox dep WHERE dep.topic='dependency.request'
                AND dep.action_id=o.action_id AND dep.delivered=0))
            AND (r.event_id IS NULL OR r.available <= ?) ORDER BY o.id""",
            (*topics, now)).fetchall()
        row = next((candidate for candidate in candidates if self._claimable(db, now, candidate)), None)
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
        delivery = {"delivery_id": receipt["delivery_id"], "lease_token": token,
                    "expires_at": expires, "expires_in": self.lease, "attempt": receipt["attempts"] + 1,
                    "topic": row["topic"],
                    "event_id": row["id"], "payload": payload}
        if row["topic"] in EXECUTOR_TOPICS:
            delivery["fence_epoch"] = self.store.claim_target(payload["proposal"]["target"], identity,
                                                              receipt["delivery_id"], row["id"])
        # The span that enqueued the command, so the worker continues its trace.
        context = parse_traceparent(row["trace_context"])
        if context is not None:
            delivery["traceparent"] = context.traceparent
        return {"delivery": delivery}

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
            # expires_in is a duration on the Coordinator's clock, so the worker
            # never compares expires_at with its own clock.
            return {"expires_at": expires, "expires_in": self.lease}

    def finish(self, identity, role, delivery_id, token, result, *, retry=False, report=None, clock_estimate=None):
        with self.transaction() as (db, now):
            self._record_clock(identity, clock_estimate, now)
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
            if row["topic"] in EXECUTOR_TOPICS:
                # Only the workload's current owner reports, and a seal reports the epoch it was given.
                epoch = self._owner_epoch(row, identity)
                if epoch is None or result["kind"] == "fence_result" and result["epoch"] != epoch:
                    raise ProtocolError(409, "Stale ownership epoch")
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
            try:
                # The result answers this outbox row: ordering uses its sequence number.
                record_message(result, self.store, answers=row["event_id"], producer=identity,
                               stale_replans=self.stale_replans)
            except SupersededResult as error:
                # 409, so the worker releases the delivery instead of resending it.
                raise ProtocolError(409, "Result answers a superseded request") from error
            if row["topic"] == "executor.request" and result["status"] == "FAILED" and row["attempts"] > 1:
                # FAILED means this holder wrote nothing, with the version maybe unchanged, while an earlier
                # holder of this command may still be inside its write: seal before anything else runs.
                # CONFLICT and SUPERSEDED need no seal: the version already moved, so no such write can commit.
                self.store.require_seal(self.store.action(source["action_id"]))
            acknowledge(self.store, row["event_id"])
            db.execute("UPDATE transport_receipts SET finished=?, result_digest=? WHERE event_id=?",
                       (now, result_digest, row["event_id"]))
            return {"accepted": True, "replay": False}

    def ingest(self, role, message, identity=None):
        validate_message(message)
        if message["kind"] != INGRESS.get(role):
            raise ProtocolError(403, "Role cannot submit this message")
        with self.transaction():
            changed = record_message(message, self.store, producer=identity)
        return {"accepted": True, "replay": not changed}
