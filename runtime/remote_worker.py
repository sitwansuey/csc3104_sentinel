"""Durable HTTP component binding; no Coordinator database is opened by workers."""
from copy import deepcopy
import json
import math
import sqlite3
import threading
import time

from runtime.worker_http import StoreWatch
from runtime.worker_transport import ProtocolError, delivery_ids, digest
from shared import log, tracing
from shared.canonical import canonical_json as encoded
from shared.contracts import validate_message

LOG = log.get_logger("remote_worker")


class WorkerJournal:
    """Transport state only. Component journals remain authoritative for effects.

    It also remembers the Coordinator database position (generation and
    commit counter) at the latest accepted result, the position at which
    each result was accepted, and a detected store change until an operator
    accepts it (scripts/coordinator_restore_check.py reads all three).
    """
    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("BEGIN IMMEDIATE")  # A running worker and --accept-store may open it together.
        try:
            self.db.execute("""CREATE TABLE IF NOT EXISTS worker_deliveries (
                request_key TEXT PRIMARY KEY, topic TEXT NOT NULL, payload TEXT NOT NULL,
                delivery TEXT, result TEXT, report TEXT, accepted INTEGER NOT NULL DEFAULT 0
            )""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(worker_deliveries)")}
            for name, kind in (("accepted_generation", "TEXT"), ("accepted_commit", "INTEGER")):
                if name not in columns:  # Journals written before P3.06.
                    self.db.execute(f"ALTER TABLE worker_deliveries ADD COLUMN {name} {kind}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS coordinator_store (
                id INTEGER PRIMARY KEY CHECK (id = 1), generation TEXT, commit_counter INTEGER, change TEXT
            )""")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            self.db.close()
            raise

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

    def accepted(self, key, position=None, *, remember=False):
        """Mark a result acknowledged, with the Coordinator position of the acknowledging reply.

        With remember, also keep that position as the furthest seen
        (remember_store), in the same transaction, so a crash cannot leave
        the result acknowledged and the remembered position behind it.
        """
        generation, commit = position or (None, None)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("UPDATE worker_deliveries SET accepted=1,accepted_generation=?,accepted_commit=? "
                            "WHERE request_key=?", (generation, commit, key))
            if remember and position is not None:
                self.remember_store(generation, commit)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def store_position(self):
        """(generation, commit counter, change): what this worker remembers of the Coordinator's database."""
        row = self.db.execute("SELECT generation, commit_counter, change FROM coordinator_store WHERE id=1").fetchone()
        if row is None:
            return None, None, None
        return row[0], row[1], json.loads(row[2]) if row[2] else None

    def remember_store(self, generation, commit):
        """Keep the furthest position seen for this generation; a new generation replaces it."""
        self.db.execute("""INSERT INTO coordinator_store(id,generation,commit_counter) VALUES(1,?,?)
            ON CONFLICT(id) DO UPDATE SET commit_counter=CASE WHEN generation IS excluded.generation
                THEN max(coalesce(commit_counter, 0), excluded.commit_counter) ELSE excluded.commit_counter END,
            generation=excluded.generation""", (generation, commit))

    def record_store_change(self, change):
        """Keep the first detected change; the worker claims nothing new while it is set."""
        self.db.execute("""INSERT INTO coordinator_store(id,change) VALUES(1,?)
            ON CONFLICT(id) DO UPDATE SET change=coalesce(change, excluded.change)""", (encoded(change),))

    def accept_store(self):
        """Clear a recorded change and the remembered position (an operator's decision); return the change."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            change = self.store_position()[2]
            self.db.execute("UPDATE coordinator_store SET generation=NULL, commit_counter=NULL, change=NULL")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        return change


def lease_duration(answer, clock=time.time):
    """Seconds left on a lease, as measured by the Coordinator.

    expires_in is a duration on the Coordinator's clock, so worker clock skew
    cannot change it. A Coordinator older than expires_in sends only
    expires_at, which is then compared with this worker's clock.
    """
    if isinstance(answer.get("expires_in"), (int, float)) and math.isfinite(answer["expires_in"]):
        return answer["expires_in"]
    return answer["expires_at"] - clock()


class Lease:
    """Renew network delivery during long inference/collection; fail closed on loss.

    Renews every interval seconds, or three times per lease when the lease is
    shorter, measured from the delivery's expires_in.
    """
    def __init__(self, client, delivery, *, interval=5, clock=time.time):
        if not math.isfinite(interval) or interval <= 0:
            raise ValueError("Renewal interval must be positive and finite")
        self.client, self.delivery = client, delivery
        self.body = {key: delivery[key] for key in ("delivery_id", "lease_token")}
        self.interval = min(interval, max(0.05, lease_duration(delivery, clock) / 3))
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

    max_clock_skew (seconds), when set, is the skew bound: the worker claims
    new work only while its measured offset from the Coordinator's clock plus
    the measurement's uncertainty is within it. Saved results are still sent,
    since their order is decided by Coordinator sequence, not by clocks.
    The claim's own reply is measured too: a clock that stepped out of bound
    since the previous exchange hands the claimed delivery back unprocessed.
    A step while a claimed request is being processed is seen only at the
    next exchange (a lease renewal or the result).

    The worker also stops claiming once a Coordinator reply shows its
    database was replaced or rolled back (store_status), and stays stopped,
    across restarts, until an operator runs worker_service --accept-store.
    Saved results are still sent and never dropped.

    Each pass therefore runs, in order: re-send a saved result (a pass
    whose re-send is accepted ends there); refuse new work while a store
    change is recorded (the journal and the previous reply, no request);
    refuse it while the clock is outside the skew bound (at most a /clock
    exchange); then run before_claim; then let the component sweep its own
    in-doubt state (the Executor's ledger rows) and claim. The cheap checks
    come first, so a worker that would refuse the claim anyway never runs
    its preflight or its sweep. A seal delivery (executor.seal) goes through
    the same pass as any other Executor command: its saved fence_result is
    re-sent first and a new seal is claimed only past the same gates.
    """

    def __init__(self, client, journal, adapter, *, renewal_interval=5, max_clock_skew=None,
                 before_claim=None):
        if max_clock_skew is not None and (not math.isfinite(max_clock_skew) or max_clock_skew <= 0):
            raise ValueError("The clock skew bound must be positive and finite")
        self.client, self.journal, self.adapter = client, journal, adapter
        self.renewal_interval = renewal_interval
        self.max_clock_skew = max_clock_skew
        self.before_claim = before_claim
        # IDs of the delivery handled by the latest once(), for status log lines.
        self.current = {}
        # The recorded store change in force, or None while claiming is allowed.
        self.store_change = None
        watch = self._watch()
        if watch is not None:
            generation, commit, change = journal.store_position()
            watch.seed(generation, commit, change)
            self.store_change = change

    def _watch(self):
        watch = getattr(self.client, "store", None)
        return watch if isinstance(watch, StoreWatch) else None  # Stand-in clients have none.

    def store_status(self):
        """None while claiming is allowed, else a store_changed status (fail closed).

        A change seen in a reply is recorded in the journal and logged once.
        When an operator clears it (worker_service --accept-store, after
        scripts/coordinator_restore_check.py), the next reply's database is
        adopted and claiming resumes.
        """
        watch = self._watch()
        if watch is None:
            return None
        marker = self.journal.store_position()[2]
        change = watch.change
        if change is not None and marker is None:
            if self.store_change is not None:
                watch.reset()
                self.store_change = None
                log.emit(LOG, "worker.store_change_accepted", reason=change["reason"])
                return None
            self.journal.record_store_change(change)
            marker = change
            log.emit(LOG, "worker.store_generation_changed" if change["reason"] == "generation_changed"
                     else "worker.store_rollback_detected", level=log.ERROR, reason=change["reason"],
                     store_generation=change["generation"], commit_counter=change["commit"],
                     previous_generation=change["previous_generation"], previous_commit=change["previous_commit"])
        elif marker is not None and change is None:
            watch.seed(watch.generation, watch.highest, marker)  # Recorded by another process on this journal.
        self.store_change = marker
        return None if marker is None else {"status": "store_changed", "reason": marker["reason"]}

    def store_ready(self):
        """For /readyz: False while a store change stops this worker from claiming."""
        return self.store_change is None

    def _store_changed_since_claim(self, delivery):
        """None, or a store_changed status after handing back a delivery whose reply revealed the change."""
        watch = self._watch()
        if watch is None or watch.change is None:
            return None
        status = self.store_status()
        if status is not None and delivery is not None:
            self.client.post("retry", {name: delivery[name] for name in ("delivery_id", "lease_token")})
        return status

    def clock_status(self):
        """None when claiming is allowed, else a clock_skew status (fail closed)."""
        estimator = getattr(self.client, "clock", None)
        if self.max_clock_skew is None:
            if estimator is not None and estimator.estimate() is None:
                # Measure once first: a Coordinator with its own bound refuses a claim without an estimate.
                self.client.post("clock", {})
            return None
        if estimator is None:
            return {"status": "clock_skew", "offset": None, "uncertainty": None}
        if not estimator.within(self.max_clock_skew):
            # Measure again before the first claim and on every pass while out
            # of bound, so a corrected clock recovers.
            self.client.post("clock", {})
        if estimator.within(self.max_clock_skew):
            return None
        self.client.post("clock", {})  # Report the refusing estimate, so the Coordinator records it.
        return self._skew_status(estimator)

    def clock_ready(self):
        """For /readyz: False while a bound is set and the latest measurement is outside it."""
        if self.max_clock_skew is None:
            return True
        estimator = getattr(self.client, "clock", None)
        return estimator is not None and estimator.within(self.max_clock_skew)

    @staticmethod
    def _skew_status(estimator):
        estimate = estimator.estimate()
        return {"status": "clock_skew", "offset": estimate and round(estimate[0], 3),
                "uncertainty": estimate and round(estimate[1], 3)}

    def _skewed_since_claim(self, delivery):
        """None, or a clock_skew status after handing back a delivery claimed on a clock that just stepped."""
        estimator = getattr(self.client, "clock", None)
        if self.max_clock_skew is None or estimator is None or estimator.within(self.max_clock_skew):
            return None
        if delivery is not None:
            self.client.post("retry", {name: delivery[name] for name in ("delivery_id", "lease_token")})
        return self._skew_status(estimator)

    def _sweep(self):
        """Let the component settle its own in-doubt state before claiming (the Executor's ledger rows).

        No command for a row may ever come back to this host once a peer
        finished it, so this runs on every pass that may claim, on the first
        such pass after a restart included; the component backs off rows it
        cannot decide. A pass whose re-sent saved result is accepted, or that
        refuses new work for a store change or clock skew, ends before the sweep.
        """
        sweep = getattr(self.adapter, "sweep", None)
        if sweep is None:
            return
        try:
            sweep()
        except Exception as error:  # noqa: BLE001 - a failed sweep must not stop claiming; the next pass retries
            log.failure(LOG, "worker.sweep.failed", error)

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
        watch = self._watch()
        position = watch.last_seen() if watch is not None else None  # This thread's result reply.
        # A restarted worker compares later replies with the remembered position.
        self.journal.accepted(row["request_key"], position, remember=watch is not None and watch.change is None)
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
        changed = self.store_status()  # After the saved results: those are always sent.
        if changed is not None:
            return changed
        skewed = self.clock_status()
        if skewed is not None:
            return skewed
        deferred = self.before_claim is not None and self.journal.held()
        if self.before_claim is not None and not deferred:
            self.before_claim()
        self._sweep()
        delivery = self.client.post("claim", {})["delivery"]
        changed = self._store_changed_since_claim(delivery)
        if changed is not None:
            return changed
        skewed = self._skewed_since_claim(delivery)
        if skewed is not None:
            return skewed
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
