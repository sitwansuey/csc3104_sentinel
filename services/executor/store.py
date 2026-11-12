"""Durable execution records and crash-released, single-host target locks.

Every worker must use the same ledger and lock directory on a local filesystem.
These locks do not implement distributed leases or fencing across hosts.
The ledger is never the cross-host source of truth: reconciliation recognises a
write by the evidence on the Deployment itself, so any host can reconcile.
"""

from contextlib import contextmanager
from datetime import timedelta, timezone
import errno
import json
import os
from pathlib import Path
import sqlite3

# canonical_json and fingerprint are public names; the adapter, harnesses and tests import them.
from shared.actions import RESTART_HISTORY_LIMIT
from shared.canonical import canonical_json, sha256_hex as fingerprint


RECONCILE_BACKOFF_MIN_SECONDS = 2
RECONCILE_BACKOFF_MAX_SECONDS = 60
_ADDED_COLUMNS = (("write_started_at", "TEXT"),
                  ("reconcile_attempts", "INTEGER NOT NULL DEFAULT 0"),
                  ("next_reconcile_at", "TEXT"))


def _stamp(moment):
    """Fixed-width UTC text, so stored times compare correctly as strings."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class ActionConflict(ValueError):
    """An existing action ID was reused with different proposal content."""


class TargetBusy(RuntimeError):
    """Another local worker owns this workload; leave delivery pending."""


def target_key(target):
    return fingerprint({field: target[field] for field in ("cluster_id", "namespace", "kind", "name")})


class LocalTargetCoordinator:
    """Nonblocking OS locks, automatically released when a process dies.

    Exclude UID from the key so a recreated Deployment shares the same lock.
    Keep lock files in place: unlinking an owned file can create two lock domains.
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def hold(self, target):
        path = self.directory / (target_key(target) + ".lock")
        with path.open("a+b") as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise TargetBusy("Workload is owned by another local executor") from exc
                raise
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SQLiteExecutionLedger:
    """Independent of Coordinator storage; use one connection per worker."""

    def __init__(self, path):
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS executor_actions (
                action_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
                proposal TEXT NOT NULL, target_key TEXT NOT NULL, phase TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                expected_version TEXT, evidence TEXT, guard_result TEXT, result TEXT,
                delivered INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS executor_attempts (
                action_id TEXT NOT NULL REFERENCES executor_actions(action_id),
                number INTEGER NOT NULL, expected_version TEXT NOT NULL,
                evidence TEXT NOT NULL, guard_result TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT,
                PRIMARY KEY (action_id, number)
            );
            CREATE TABLE IF NOT EXISTS executor_results (
                id INTEGER PRIMARY KEY,
                action_id TEXT NOT NULL REFERENCES executor_actions(action_id),
                result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS executor_fences (
                action_id TEXT NOT NULL REFERENCES executor_actions(action_id),
                nonce TEXT NOT NULL, started_at TEXT NOT NULL, committed_version TEXT,
                PRIMARY KEY (action_id, nonce)
            );
        """)
        self._migrate()

    def _migrate(self):
        """Add reconcile bookkeeping columns to a ledger created by an older schema."""
        with self._transaction():
            present = {row[1] for row in self.db.execute("PRAGMA table_info(executor_actions)")}
            for name, definition in _ADDED_COLUMNS:
                if name not in present:
                    self.db.execute(f"ALTER TABLE executor_actions ADD COLUMN {name} {definition}")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

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
        row = self.db.execute("SELECT * FROM executor_actions WHERE action_id=?", (action_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("proposal", "evidence", "guard_result", "result"):
            result[key] = json.loads(result[key]) if result[key] is not None else None
        return result

    def prepare(self, proposal):
        """Claim immutable identity before reading any execution dependencies."""
        encoded = canonical_json(proposal)
        digest = fingerprint(proposal)
        with self._transaction():
            row = self.get(proposal["action_id"])
            if row is not None:
                if row["fingerprint"] != digest or canonical_json(row["proposal"]) != encoded:
                    raise ActionConflict("Action ID is already bound to a different proposal")
            if row is None or row["phase"] == "PREPARED":
                uncertain = self.db.execute(
                    "SELECT action_id FROM executor_actions WHERE target_key=? "
                    "AND action_id<>? AND phase IN ('WRITE_STARTED','UNCERTAIN')",
                    (target_key(proposal["target"]), proposal["action_id"]),
                ).fetchone()
                if uncertain is not None:
                    raise TargetBusy("Workload has an unresolved execution; reconcile it first")
            if row is None:
                self.db.execute(
                    "INSERT INTO executor_actions(action_id,fingerprint,proposal,target_key,phase) "
                    "VALUES(?,?,?,?,'PREPARED')",
                    (proposal["action_id"], digest, encoded, target_key(proposal["target"])),
                )
        return self.get(proposal["action_id"])

    def record_validation(self, action_id, decision):
        self.db.execute("UPDATE executor_actions SET guard_result=? WHERE action_id=?",
                        (canonical_json(decision), action_id))

    def start_write(self, action_id, version, evidence, decision, started_at=None):
        """Commit the uncertain boundary BEFORE calling the external API."""
        with self._transaction():
            row = self.get(action_id)
            if row["phase"] != "PREPARED":
                raise RuntimeError("An uncertain or completed action cannot start another write")
            number = row["attempts"] + 1
            encoded_evidence, encoded_decision = canonical_json(evidence), canonical_json(decision)
            self.db.execute(
                "UPDATE executor_actions SET phase='WRITE_STARTED',attempts=?,expected_version=?,"
                "evidence=?,guard_result=?,write_started_at=? WHERE action_id=?",
                (number, version, encoded_evidence, encoded_decision,
                 _stamp(started_at) if started_at is not None else None, action_id),
            )
            self.db.execute(
                "INSERT INTO executor_attempts(action_id,number,expected_version,evidence,guard_result,status) "
                "VALUES(?,?,?,?,?,'WRITE_STARTED')",
                (action_id, number, version, encoded_evidence, encoded_decision),
            )

    def adopt(self, proposal, now):
        """Journal an action this host never wrote as in doubt, for stateless reconcile.

        The row has no attempt and expects the proposal's resource version, the
        only version at which any host's execute() writes. Once adopted, this
        host can only reconcile the action, never start its write.
        """
        encoded = canonical_json(proposal)
        digest = fingerprint(proposal)
        with self._transaction():
            row = self.get(proposal["action_id"])
            if row is not None and (row["fingerprint"] != digest or canonical_json(row["proposal"]) != encoded):
                raise ActionConflict("Action ID is already bound to a different proposal")
            if row is None:
                self.db.execute(
                    "INSERT INTO executor_actions(action_id,fingerprint,proposal,target_key,phase,"
                    "expected_version,write_started_at) VALUES(?,?,?,?,'WRITE_STARTED',?,?)",
                    (proposal["action_id"], digest, encoded, target_key(proposal["target"]),
                     proposal["based_on_resource_version"], _stamp(now)),
                )
            elif row["phase"] == "PREPARED":
                self.db.execute(
                    "UPDATE executor_actions SET phase='WRITE_STARTED',expected_version=?,write_started_at=? "
                    "WHERE action_id=?",
                    (proposal["based_on_resource_version"], _stamp(now), proposal["action_id"]),
                )
        return self.get(proposal["action_id"])

    def schedule_reconcile(self, action_id, now):
        """Back off the next reconcile pass: 2 s, doubling, capped at 60 s."""
        with self._transaction():
            row = self.db.execute("SELECT reconcile_attempts FROM executor_actions WHERE action_id=?",
                                  (action_id,)).fetchone()
            if row is None:
                return
            attempts = row[0] + 1
            delay = min(RECONCILE_BACKOFF_MAX_SECONDS, RECONCILE_BACKOFF_MIN_SECONDS * 2 ** min(attempts - 1, 6))
            self.db.execute(
                "UPDATE executor_actions SET reconcile_attempts=?,next_reconcile_at=?,"
                "write_started_at=COALESCE(write_started_at,?) WHERE action_id=?",
                (attempts, _stamp(now + timedelta(seconds=delay)), _stamp(now), action_id),
            )

    def record_fence(self, action_id, nonce, started_at):
        """Journal a fence nonce BEFORE the fence is sent, like start_write.

        Only an action still in doubt can be fenced. A crash after this commit
        leaves the nonce journaled, so a fence that did land is still recognised.
        """
        with self._transaction():
            row = self.db.execute("SELECT phase FROM executor_actions WHERE action_id=?", (action_id,)).fetchone()
            if row is None or row[0] not in ("WRITE_STARTED", "UNCERTAIN"):
                raise RuntimeError("Only an action still in doubt can be fenced")
            self.db.execute("INSERT INTO executor_fences(action_id,nonce,started_at) VALUES(?,?,?)",
                            (action_id, nonce, _stamp(started_at)))

    def confirm_fence(self, action_id, nonce, version):
        """Record that a fence's own success response showed it committed at version."""
        self.db.execute("UPDATE executor_fences SET committed_version=? WHERE action_id=? AND nonce=?",
                        (version, action_id, nonce))

    def fences(self, action_id):
        """Journaled fences of the action, in the order they were journaled."""
        return [dict(row) for row in self.db.execute(
            "SELECT nonce,started_at,committed_version FROM executor_fences WHERE action_id=? ORDER BY rowid",
            (action_id,))]

    def reconcile_due(self, action_id, now):
        """True unless an earlier reconcile pass scheduled the next one later than now."""
        row = self.db.execute("SELECT next_reconcile_at FROM executor_actions WHERE action_id=?",
                              (action_id,)).fetchone()
        return row is None or row[0] is None or row[0] <= _stamp(now)

    def finish(self, action_id, result):
        phase = "UNCERTAIN" if result["status"] == "NEEDS_RECONCILIATION" else "DONE"
        encoded = canonical_json(result)
        with self._transaction():
            self.db.execute("UPDATE executor_actions SET phase=?,result=?,delivered=0 WHERE action_id=?",
                            (phase, encoded, action_id))
            self.db.execute("INSERT INTO executor_results(action_id,result) VALUES(?,?)", (action_id, encoded))
            self.db.execute(
                "UPDATE executor_attempts SET status=?,result=? WHERE action_id=? AND number="
                "(SELECT attempts FROM executor_actions WHERE action_id=?)",
                (result["status"], encoded, action_id, action_id),
            )
        return result

    def attempts(self, action_id):
        rows = self.db.execute("SELECT * FROM executor_attempts WHERE action_id=? ORDER BY number", (action_id,))
        result = []
        for row in rows:
            item = dict(row)
            for key in ("evidence", "guard_result", "result"):
                item[key] = json.loads(item[key]) if item[key] is not None else None
            result.append(item)
        return result

    def restart_history(self, target):
        """The newest RESTART writes this ledger started for the workload, oldest first.

        The workload key ignores the UID, like the target lock, so recreating a
        Deployment does not reset its restart budget. A write counts once it has
        started, whatever its outcome: one still in doubt may have restarted Pods.
        Adopted rows (another host's write reconciled here) count too. At most
        RESTART_HISTORY_LIMIT entries are returned, so a long-lived target is
        not escalated forever for having a long history; older entries cannot
        change the budget decision (see shared/actions.py).
        """
        history = []
        for row in self.db.execute(
                "SELECT action_id,proposal,write_started_at FROM executor_actions "
                "WHERE target_key=? AND write_started_at IS NOT NULL ORDER BY write_started_at DESC,rowid DESC",
                (target_key(target),)):
            if json.loads(row["proposal"]).get("action") == "RESTART":
                history.append({"action_id": row["action_id"], "started_at": row["write_started_at"]})
                if len(history) == RESTART_HISTORY_LIMIT:
                    break
        history.reverse()
        return history

    def results(self, action_id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT result FROM executor_results WHERE action_id=? ORDER BY id", (action_id,)
        )]

    def pending_proposals(self, due_at=None):
        """Find uncertain writes even if their routing command was canceled.

        With due_at, skip rows whose reconcile backoff ends after that time.
        """
        if due_at is None:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT proposal FROM executor_actions WHERE phase IN ('WRITE_STARTED','UNCERTAIN') ORDER BY rowid"
            )]
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT proposal FROM executor_actions WHERE phase IN ('WRITE_STARTED','UNCERTAIN') "
            "AND (next_reconcile_at IS NULL OR next_reconcile_at<=?) ORDER BY rowid", (_stamp(due_at),)
        )]

    def undelivered_results(self):
        """Durable result outbox, including completed actions with canceled commands."""
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT result FROM executor_actions WHERE result IS NOT NULL AND delivered=0 ORDER BY rowid"
        )]

    def acknowledge_result(self, result):
        """Call AFTER Coordinator delivery or review commits, never before.

        Matching the exact result prevents an old worker acknowledging a newer
        reconciliation result. A crash before this acknowledgment only repeats
        delivery; it cannot lose the independently committed execution evidence.
        """
        cursor = self.db.execute(
            "UPDATE executor_actions SET delivered=1 WHERE action_id=? AND result=?",
            (result["action_id"], canonical_json(result)),
        )
        return cursor.rowcount == 1
