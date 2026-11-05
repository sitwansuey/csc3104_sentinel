"""Durable execution records and crash-released, single-host target locks.

Every worker must use the same ledger and lock directory on a local filesystem.
These locks do not implement distributed leases or fencing across hosts.
"""

from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3


class ActionConflict(ValueError):
    """An existing action ID was reused with different proposal content."""


class TargetBusy(RuntimeError):
    """Another local worker owns this workload; leave delivery pending."""


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


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
        """)

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

    def start_write(self, action_id, version, evidence, decision):
        """Commit the uncertain boundary BEFORE calling the external API."""
        with self._transaction():
            row = self.get(action_id)
            if row["phase"] != "PREPARED":
                raise RuntimeError("An uncertain or completed action cannot start another write")
            number = row["attempts"] + 1
            encoded_evidence, encoded_decision = canonical_json(evidence), canonical_json(decision)
            self.db.execute(
                "UPDATE executor_actions SET phase='WRITE_STARTED',attempts=?,expected_version=?,"
                "evidence=?,guard_result=? WHERE action_id=?",
                (number, version, encoded_evidence, encoded_decision, action_id),
            )
            self.db.execute(
                "INSERT INTO executor_attempts(action_id,number,expected_version,evidence,guard_result,status) "
                "VALUES(?,?,?,?,?,'WRITE_STARTED')",
                (action_id, number, version, encoded_evidence, encoded_decision),
            )

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

    def results(self, action_id):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT result FROM executor_results WHERE action_id=? ORDER BY id", (action_id,)
        )]

    def pending_proposals(self):
        """Find uncertain writes even if their routing command was canceled."""
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT proposal FROM executor_actions WHERE phase IN ('WRITE_STARTED','UNCERTAIN') ORDER BY rowid"
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
