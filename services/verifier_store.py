"""Single-host verification checkpoints, independent of Coordinator storage."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import sqlite3

from services.verifier import VerifierInputError, verify


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    except ValueError as exc:
        raise VerifierInputError("Expected a UTC timestamp") from exc
    if not isinstance(stamp, datetime) or stamp.utcoffset() != timedelta(0):
        raise VerifierInputError("Expected a timezone-aware UTC timestamp")
    return stamp


def _time(value):
    return value.isoformat().replace("+00:00", "Z")


class VerificationStore:
    """One connection per local worker; this is not cross-host coordination."""

    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.execute("""CREATE TABLE IF NOT EXISTS verification_jobs (
            action_id TEXT PRIMARY KEY, execution TEXT NOT NULL, policy TEXT NOT NULL,
            started_at TEXT NOT NULL, deadline TEXT NOT NULL,
            samples TEXT NOT NULL DEFAULT '[]', outcome TEXT,
            delivered INTEGER NOT NULL DEFAULT 0
        )""")

    def close(self):
        self.db.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def get(self, action_id):
        row = self.db.execute("SELECT * FROM verification_jobs WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        job = dict(row)
        for key in ("execution", "policy", "samples", "outcome"):
            job[key] = json.loads(job[key]) if job[key] is not None else None
        return job

    def start(self, execution, policy, *, now):
        verify(execution, [], policy)  # Validate the existing input contract and health rules.
        if execution["status"] not in {"APPLIED", "NOOP"} or not execution["resource_version"]:
            raise VerifierInputError("Verification requires an applied/no-op execution")
        now = _utc(now)
        if now < _utc(execution["recorded_at"]):
            raise VerifierInputError("Monitoring cannot start before execution")
        health = policy["health"]
        timeout = health.get("verification_timeout_seconds",
                             health["stable_window_seconds"] + health["minimum_samples"] * health["max_sample_gap_seconds"])
        if type(timeout) is not int or timeout < health["stable_window_seconds"]:
            raise VerifierInputError("Verification timeout must cover the healthy window")
        with self._transaction():
            job = self.get(execution["action_id"])
            if job:
                if _json(job["execution"]) != _json(execution) or _json(job["policy"]) != _json(policy):
                    raise VerifierInputError("Action ID reused with changed execution or policy")
            else:
                self.db.execute("INSERT INTO verification_jobs(action_id,execution,policy,started_at,deadline) VALUES(?,?,?,?,?)",
                                (execution["action_id"], _json(execution), _json(policy),
                                 _time(now), _time(now + timedelta(seconds=timeout))))
        return self.get(execution["action_id"])

    def record_samples(self, action_id, samples, *, now):
        """Atomically merge immutable readings; redelivery is not a new sample."""
        now = _utc(now)
        if not isinstance(samples, list):
            raise VerifierInputError("Sample reader must return a list")
        with self._transaction():
            job = self.get(action_id)
            if job is None:
                raise VerifierInputError("Unknown verification job")
            if now < _utc(job["started_at"]):
                raise VerifierInputError("Clock predates monitoring start")
            if job["outcome"] is not None:
                return job
            if now >= _utc(job["deadline"]):
                return job  # A late collector response cannot extend the observation deadline.
            readings = {_utc(sample["at"]): sample for sample in job["samples"]}
            if any(at > now for at in readings):
                raise VerifierInputError("Clock predates saved readings")
            for sample in samples:
                if not isinstance(sample, dict):
                    raise VerifierInputError("Each reading must be an object")
                at = _utc(sample.get("at"))
                if at < _utc(job["execution"]["recorded_at"]) or at > now:
                    raise VerifierInputError("Reading is outside the post-action observation period")
                target = job["execution"]["target"]
                if sample.get("service") != target["name"] or (
                    "target" in sample and sample["target"] != target
                ):
                    raise VerifierInputError("Reading does not match the execution target")
                encoded = _json(sample)
                if at in readings and _json(readings[at]) != encoded:
                    raise VerifierInputError("Reading timestamp reused with changed content")
                readings[at] = sample
            if len(readings) > 10000:
                raise VerifierInputError("Verification window exceeds 10000 readings")
            ordered = [readings[at] for at in sorted(readings)]
            self.db.execute("UPDATE verification_jobs SET samples=? WHERE action_id=?", (_json(ordered), action_id))
        return self.get(action_id)

    def evaluate(self, action_id, *, now):
        """Freeze one outcome, or keep collecting until the fixed deadline."""
        now = _utc(now)
        with self._transaction():
            job = self.get(action_id)
            if job is None:
                raise VerifierInputError("Unknown verification job")
            if job["outcome"] is not None:
                return job["outcome"]
            if now < _utc(job["started_at"]) or any(_utc(row["at"]) > now for row in job["samples"]):
                raise VerifierInputError("Clock predates saved monitoring progress")
            result = verify(job["execution"], job["samples"], job["policy"])
            if result["status"] == "UNKNOWN" and now < _utc(job["deadline"]):
                return None
            self.db.execute("UPDATE verification_jobs SET outcome=? WHERE action_id=?", (_json(result), action_id))
        return result

    def pending_outcomes(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT outcome FROM verification_jobs WHERE outcome IS NOT NULL AND delivered=0 ORDER BY action_id"
        )]

    def acknowledge_outcome(self, outcome):
        """Acknowledge only after Coordinator durably accepts this exact result."""
        with self._transaction():
            job = self.get(outcome["action_id"])
            if job is None or job["outcome"] is None or _json(job["outcome"]) != _json(outcome):
                raise VerifierInputError("Acknowledgment does not match the saved outcome")
            self.db.execute("UPDATE verification_jobs SET delivered=1 WHERE action_id=?", (outcome["action_id"],))
