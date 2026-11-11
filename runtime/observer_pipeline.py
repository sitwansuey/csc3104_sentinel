"""Durable Observer checkpoints/outbox and local Coordinator integration."""

from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from services.observer.core import Observer, validate_observation
from shared.contracts import validate_message


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class ObserverStore:
    """Atomic detection checkpoint plus pending messages, on one host/PVC."""
    def __init__(self, path, policy, target):
        self.policy, self.target = deepcopy(policy), deepcopy(target)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS observer_checkpoint (
                id INTEGER PRIMARY KEY CHECK(id=1), configuration TEXT NOT NULL,
                state TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observer_outbox (
                incident_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS observer_incident_context (
                incident_id TEXT PRIMARY KEY, payload TEXT NOT NULL
            );
        """)
        fingerprint = hashlib.sha256(encoded({"policy": policy, "target": target}).encode()).hexdigest()
        initial_status = {"polls": 0, "collection_errors": 0, "last_success_at": None,
                          "last_collection_error": None, "last_delivery_error": None}
        self.db.execute("INSERT OR IGNORE INTO observer_checkpoint VALUES(1,?,?,?)",
                        (fingerprint, encoded(Observer(policy).export_state()), encoded(initial_status)))
        if self.db.execute("SELECT configuration FROM observer_checkpoint WHERE id=1").fetchone()[0] != fingerprint:
            self.close()
            raise ValueError("Observer state belongs to a different target or policy; preserve it and choose a new state file")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    def _load(self):
        row = self.db.execute("SELECT state,status FROM observer_checkpoint WHERE id=1").fetchone()
        return Observer.from_state(self.policy, json.loads(row["state"])), json.loads(row["status"])

    def _save(self, observer, status):
        self.db.execute("UPDATE observer_checkpoint SET state=?,status=? WHERE id=1",
                        (encoded(observer.export_state()), encoded(status)))

    def observe(self, observation):
        snapshot = validate_observation(observation)
        if any(snapshot.target.get(key) != value for key, value in self.target.items()):
            raise ValueError("Observation does not match the configured Observer target")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            observer, status = self._load()
            incident = observer.observe(observation)
            if incident:
                validate_message(incident)
                self.db.execute("INSERT INTO observer_outbox(incident_id,payload) VALUES(?,?)",
                                (incident["incident_id"], encoded(incident)))
                if "telemetry" in observation:
                    self.db.execute("INSERT INTO observer_incident_context VALUES(?,?)",
                                    (incident["incident_id"], encoded(observation["telemetry"])))
            status.update(polls=status["polls"] + 1, last_success_at=observation["captured_at"],
                          last_collection_error=None)
            self._save(observer, status)
            self.db.execute("COMMIT")
            return incident
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def collection_failed(self, error):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            observer, status = self._load()
            observer.collection_gap()
            status.update(collection_errors=status["collection_errors"] + 1,
                          last_collection_error=type(error).__name__)
            self._save(observer, status)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT payload FROM observer_outbox WHERE delivered=0 ORDER BY rowid")]

    def incident_context(self, incident_id):
        row = self.db.execute("SELECT payload FROM observer_incident_context WHERE incident_id=?", (incident_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def flush(self, sink):
        delivered = 0
        for incident in self.pending():
            try:
                sink(deepcopy(incident))
            except Exception as error:
                self._delivery_status(type(error).__name__)
                return {"delivered": delivered, "error": type(error).__name__}
            # A crash before this acknowledgment replays the identical message.
            self.db.execute("UPDATE observer_outbox SET delivered=1 WHERE incident_id=?", (incident["incident_id"],))
            delivered += 1
        self._delivery_status(None)
        return {"delivered": delivered, "error": None}

    def _delivery_status(self, error):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            observer, status = self._load()
            status["last_delivery_error"] = error
            self._save(observer, status)
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def status(self):
        return read_status(self.db)


def read_status(connection):
    row = connection.execute("SELECT state,status FROM observer_checkpoint WHERE id=1").fetchone()
    state, status = json.loads(row[0]), json.loads(row[1])
    status["failure_duration_seconds"] = state["failure_duration_seconds"]
    status["max_sample_gap_seconds"] = state["max_sample_gap_seconds"]
    status["incidents"] = connection.execute("SELECT count(*) FROM observer_outbox").fetchone()[0]
    status["pending"] = connection.execute("SELECT count(*) FROM observer_outbox WHERE delivered=0").fetchone()[0]
    return status


def status_file(path):
    # Readiness probes must not create a missing database or contend as writers.
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        return read_status(connection)


def is_ready(status, *, now=None):
    if not status["last_success_at"] or status["last_collection_error"] or status["last_delivery_error"]:
        return False
    now = now or datetime.now(timezone.utc)
    captured = datetime.fromisoformat(status["last_success_at"].replace("Z", "+00:00"))
    age = (now - captured).total_seconds()
    return 0 <= age <= status["max_sample_gap_seconds"]


class LocalCoordinatorSink:
    """Runtime wiring uses Owner 1's public ingestion interface without running consumers."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, incident):
        from services.coordinator.core import SQLiteStore, record_message
        with SQLiteStore(self.path) as store, store.transaction():
            record_message(incident, store)
