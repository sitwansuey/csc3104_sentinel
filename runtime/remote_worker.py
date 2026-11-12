"""Durable HTTP component binding; no Coordinator database is opened by workers."""
from copy import deepcopy
import json
import math
import sqlite3
import threading
import time

from runtime.worker_transport import ProtocolError, delivery_ids, digest
from shared import log, tracing
from shared.canonical import canonical_json as encoded
from shared.contracts import validate_message

LOG = log.get_logger("remote_worker")


class WorkerJournal:
    """Transport state only. Component journals remain authoritative for effects."""
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS worker_deliveries (
            request_key TEXT PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
            delivery TEXT, result TEXT, report TEXT, accepted INTEGER NOT NULL DEFAULT 0
        )""")

    def close(self):
        self.db.close()

    def bind(self, delivery):
        key = digest({"topic": delivery["topic"], "payload": delivery["payload"]})
        self.db.execute("""INSERT INTO worker_deliveries(request_key,topic,payload,delivery)
            VALUES(?,?,?,?) ON CONFLICT(request_key) DO UPDATE SET delivery=excluded.delivery,accepted=0""",
            (key, delivery["topic"], encoded(delivery["payload"]), encoded(delivery)))
        return key

    def get(self, key):
        row = self.db.execute("SELECT * FROM worker_deliveries WHERE request_key=?", (key,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for field in ("payload", "delivery", "result", "report"):
            result[field] = json.loads(result[field]) if result[field] is not None else None
        return result

    def save(self, key, result, report=None):
        validate_message(result)
        previous = self.get(key)
        if previous["result"] is not None and (previous["result"] != result or previous["report"] != report):
            raise ValueError("A saved request cannot change its result")
        self.db.execute("UPDATE worker_deliveries SET result=?,report=? WHERE request_key=?",
                        (encoded(result), encoded(report) if report is not None else None, key))

    def pending(self):
        return [self.get(row[0]) for row in self.db.execute("""SELECT request_key FROM worker_deliveries
            WHERE result IS NOT NULL AND accepted=0 AND delivery IS NOT NULL ORDER BY rowid LIMIT 1000""")]

    def held(self):
        """True while a saved result waits, unaccepted, for its request to be delivered again."""
        return self.db.execute("""SELECT 1 FROM worker_deliveries
            WHERE result IS NOT NULL AND accepted=0 AND delivery IS NULL LIMIT 1""").fetchone() is not None

    def release(self, key):
        self.db.execute("UPDATE worker_deliveries SET delivery=NULL WHERE request_key=?", (key,))

    def accepted(self, key):
        self.db.execute("UPDATE worker_deliveries SET accepted=1 WHERE request_key=?", (key,))


class Lease:
    """Renew network delivery during long inference/collection; fail closed on loss."""
    def __init__(self, client, delivery, *, interval=5):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Renewal interval must be positive and finite")
        self.client, self.delivery = client, delivery
        self.body = {key: delivery[key] for key in ("delivery_id", "lease_token")}
        self.interval = min(interval, max(0.05, (delivery["expires_at"] - time.time()) / 3))
        self.stop = threading.Event()
        self.error = None

    def ensure_current(self):
        if self.error is not None:
            raise self.error
        return self.client.post("renew", self.body)

    def _renew(self):
        while not self.stop.wait(self.interval):
            try:
                self.ensure_current()
            except Exception as error:  # noqa: BLE001 - ensure_current() raises it again in the worker
                log.failure(LOG, "transport.lease.lost", error)
                self.error = error
                return

    def __enter__(self):
        self.ensure_current()
        self.thread = log.context_thread(self._renew, daemon=True)  # Inherits correlation IDs.
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()  # HTTP requests have the client's bounded timeout.


class RemoteWorker:
    """One polling pass at a time: re-send saved results, then claim and process new work.

    before_claim, when given, is a prerequisite for new work only (the agent
    Planner's model preflight). It runs just before a claim and raises to claim
    nothing. A saved result needs no prerequisite, so it never waits for one:
    pending results are re-sent first, and while a released saved result waits
    for its request to come back the worker claims anyway, sends a saved result
    if the claim matches one, and otherwise checks the prerequisite before any
    processing and hands the request back with retry when it fails.
    """

    def __init__(self, client, journal, adapter, *, renewal_interval=5, before_claim=None):
        self.client, self.journal, self.adapter = client, journal, adapter
        self.renewal_interval = renewal_interval
        self.before_claim = before_claim
        # IDs of the delivery handled by the latest once(), for status log lines.
        self.current = {}

    def _send(self, row):
        delivery = row["delivery"]
        self.current = delivery_ids(delivery)
        # Inside worker.process this is its child. A result replayed after a
        # restart continues from the saved delivery's traceparent instead.
        parent = None if tracing.current() else tracing.parse_traceparent(delivery.get("traceparent"))
        with log.bind(**self.current), tracing.start_span("worker.result", parent=parent):
            return self._post_result(row, delivery)

    def _post_result(self, row, delivery):
        body ={key: delivery[key] for key in ("delivery_id", "lease_token")}
        body["result"] = row["result"]
        if row["report"] is not None:
            body["report"] = row["report"]
        response = self.client.post("result", body)
        if response.get("accepted") is not True:
            raise ProtocolError(502, "Coordinator did not acknowledge the saved result")
        # Repeat component acknowledgment safely if a process dies before the
        # transport marker. Neither operation may manufacture another result.
        if hasattr(self.adapter, "acknowledge"):
            self.adapter.acknowledge(deepcopy(row["result"]))
        self.journal.accepted(row["request_key"])
        return {"status": "accepted", "replay": response.get("replay", False)}

    def once(self):
        self.current = {}
        for row in self.journal.pending():
            try:
                return self._send(row)
            except ProtocolError as error:
                if error.status not in {404, 409}:
                    raise
                # Retain the immutable result for a replacement delivery, but
                # never keep retrying a lease that can no longer commit it.
                self.journal.release(row["request_key"])
        deferred = self.before_claim is not None and self.journal.held()
        if self.before_claim is not None and not deferred:
            self.before_claim()
        delivery = self.client.post("claim", {})["delivery"]
        if delivery is None:
            return {"status": "idle"}
        self.current = delivery_ids(delivery)
        key = self.journal.bind(delivery)
        row = self.journal.get(key)
        if row["result"] is not None:
            return self._send(row)
        if deferred:
            self._check_before_work(key, delivery)
        with log.bind(**self.current), tracing.start_span(
                "worker.process", parent=tracing.parse_traceparent(delivery.get("traceparent"))):
            return self._process(key, delivery)

    def _check_before_work(self, key, delivery):
        """Run before_claim for a claimed request with no saved result; hand it back if it fails."""
        try:
            self.before_claim()
        except Exception:
            try:
                self.client.post("retry", {name: delivery[name] for name in ("delivery_id", "lease_token")})
            except Exception as error:  # noqa: BLE001 - lease expiry still makes the request claimable again
                log.failure(LOG, "transport.retry.failed", error)
            self.journal.release(key)
            raise

    def _process(self, key, delivery):
        try:
            with Lease(self.client, delivery, interval=self.renewal_interval) as lease:
                output = self.adapter.process(deepcopy(delivery), lease)
                if output is not None:
                    result, report = output
                    # Save before the final lease check/HTTP delivery so a
                    # timed-out response cannot recall a provider or repeat work.
                    self.journal.save(key, result, report)
                    lease.ensure_current()
        except Exception:
            if self.journal.get(key)["result"] is None:
                try:
                    self.client.post("retry", {name: delivery[name] for name in ("delivery_id", "lease_token")})
                except Exception as error:  # noqa: BLE001 - lease expiry still makes unfinished work retryable
                    log.failure(LOG, "transport.retry.failed", error)
                self.journal.release(key)
            raise
        if output is None:
            self.client.post("retry", {name: delivery[name] for name in ("delivery_id", "lease_token")})
            self.journal.release(key)
            return {"status": "pending"}
        return self._send(self.journal.get(key))


class RemoteIngress:
    """ObserverStore.flush-compatible sink; the producer owns persist/replay."""
    def __init__(self, client):
        self.client = client

    def __call__(self, message):
        validate_message(message)
        if message["kind"] == "incident" and tracing.current() is None:
            # A new incident starts its trace here; the Coordinator continues it.
            with tracing.start_span("observer.ingest", incident_id=message["incident_id"]):
                return self._post(message)
        return self._post(message)

    def _post(self, message):
        result = self.client.post("ingest", {"message": message})
        if result.get("accepted") is not True:
            raise ProtocolError(502, "Coordinator did not acknowledge the saved message")
        return result
