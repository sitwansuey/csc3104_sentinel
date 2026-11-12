"""Single-host Planner attempt journal, separate from Coordinator state.

Save a validated proposal before delivering it. A lost Coordinator response then
replays that exact proposal without another provider call. A process death during
inference consumes an attempt and releases its lease after a bounded interval.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import secrets
import sqlite3

from shared.canonical import canonical_json as _json


class PlannerStore:
    def __init__(self, path, *, clock=None):
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE IF NOT EXISTS planning (
            incident_id TEXT PRIMARY KEY, incident TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
            lease_token TEXT, lease_until REAL NOT NULL DEFAULT 0,
            proposal TEXT, error_code TEXT, delivered INTEGER NOT NULL DEFAULT 0
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS planning_attempts (
            incident_id TEXT NOT NULL, attempt INTEGER NOT NULL, context TEXT NOT NULL,
            usage TEXT, PRIMARY KEY(incident_id,attempt)
        )""")

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
            raise ValueError("Planner clock must return a UTC datetime")
        return now.timestamp()

    def get(self, incident_id):
        row = self.db.execute("SELECT * FROM planning WHERE incident_id=?", (incident_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["incident"] = json.loads(value["incident"])
        value["proposal"] = json.loads(value["proposal"]) if value["proposal"] else None
        return value

    def claim(self, incident, *, max_attempts, lease_seconds):
        """Return (token, exhausted), or None while another attempt/backoff holds."""
        if (isinstance(max_attempts, bool) or not isinstance(max_attempts, int)
                or not 1 <= max_attempts <= 100):
            raise ValueError("Planner max_attempts must be between 1 and 100")
        if (isinstance(lease_seconds, bool) or not isinstance(lease_seconds, (int, float))
                or not math.isfinite(lease_seconds) or not 0 < lease_seconds <= 86400):
            raise ValueError("Planner lease_seconds must be positive and at most one day")
        incident_id, encoded = incident["incident_id"], _json(incident)
        now = self._now()
        with self._transaction():
            self.db.execute("INSERT OR IGNORE INTO planning(incident_id,incident) VALUES(?,?)",
                            (incident_id, encoded))
            row = self.db.execute("SELECT * FROM planning WHERE incident_id=?", (incident_id,)).fetchone()
            if row["incident"] != encoded:
                raise ValueError("Planner incident identity reused with changed payload")
            if row["proposal"] or row["next_attempt"] > now or row["lease_until"] > now:
                return None
            token = secrets.token_urlsafe(24)
            exhausted = row["attempts"] >= max_attempts
            self.db.execute("""UPDATE planning SET attempts=attempts+?,lease_token=?,lease_until=?
                               WHERE incident_id=?""",
                            (0 if exhausted else 1, token, now + lease_seconds, incident_id))
            return token, exhausted

    def save(self, incident_id, token, proposal, *, error_code=None):
        """Fence stale attempts: an expired owner cannot overwrite a newer result."""
        with self._transaction():
            cursor = self.db.execute("""UPDATE planning SET proposal=?,error_code=COALESCE(?,error_code),
                lease_token=NULL,lease_until=0
                WHERE incident_id=? AND lease_token=? AND lease_until>? AND proposal IS NULL""",
                (_json(proposal), error_code, incident_id, token, self._now()))
            if cursor.rowcount != 1:
                raise ValueError("Planner attempt lease expired or was replaced")

    def retry(self, incident_id, token, *, error_code, delay_seconds):
        if (isinstance(delay_seconds, bool) or not isinstance(delay_seconds, (int, float))
                or not math.isfinite(delay_seconds) or delay_seconds < 0):
            raise ValueError("Planner retry delay must be finite and nonnegative")
        with self._transaction():
            cursor = self.db.execute("""UPDATE planning SET error_code=?,next_attempt=?,
                lease_token=NULL,lease_until=0 WHERE incident_id=? AND lease_token=? AND lease_until>?""",
                (error_code, self._now() + delay_seconds, incident_id, token, self._now()))
            if cursor.rowcount != 1:
                raise ValueError("Planner attempt lease expired or was replaced")

    def delivered(self, incident_id):
        self.db.execute("UPDATE planning SET delivered=1 WHERE incident_id=? AND proposal IS NOT NULL",
                        (incident_id,))

    def attempt_usage(self, incident_id):
        """The saved usage of the latest recorded attempt, or None."""
        row = self.db.execute("SELECT usage FROM planning_attempts WHERE incident_id=? ORDER BY attempt DESC LIMIT 1",
                              (incident_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def save_context(self, incident_id, token, context, usage=None):
        with self._transaction():
            row = self.db.execute("SELECT * FROM planning WHERE incident_id=?", (incident_id,)).fetchone()
            if row is None or row["lease_token"] != token or row["lease_until"] <= self._now():
                raise ValueError("Planner attempt lease expired or was replaced")
            previous = self.db.execute("SELECT context FROM planning_attempts WHERE incident_id=? AND attempt=?",
                                       (incident_id, row["attempts"])).fetchone()
            encoded = _json(context)
            if previous and previous[0] != encoded:
                raise ValueError("An inference attempt cannot change its saved context")
            self.db.execute("""INSERT INTO planning_attempts(incident_id,attempt,context,usage) VALUES(?,?,?,?)
                ON CONFLICT(incident_id,attempt) DO UPDATE SET usage=COALESCE(excluded.usage,usage)""",
                (incident_id, row["attempts"], encoded, _json(usage) if usage is not None else None))

    def attempts(self, incident_id):
        return [{"attempt": row[0], "context": json.loads(row[1]),
                 "usage": json.loads(row[2]) if row[2] is not None else None} for row in self.db.execute(
            "SELECT attempt,context,usage FROM planning_attempts WHERE incident_id=? ORDER BY attempt", (incident_id,))]

    def undelivered(self):
        return [{"incident_id": row[0], "proposal": json.loads(row[1])} for row in self.db.execute(
            "SELECT incident_id,proposal FROM planning WHERE proposal IS NOT NULL AND delivered=0 LIMIT 1000")]
