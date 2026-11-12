"""scripts/coordinator_restore_check.py against small restored-database fixtures (P3.06).

Each fixture is a Coordinator database and a worker journal built with the
production store and journal code, in the state an operator would find after
a restore.
"""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from runtime.remote_worker import WorkerJournal
from scripts import coordinator_restore_check as restore_check
from services.coordinator.core import LocalEventBus, handle
from services.coordinator.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / name).read_text(encoding="utf-8"))


class RestoreCheckTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.database = self.directory / "coordinator.db"
        with SQLiteStore(self.database) as store:
            handle(sample("incident.json"), store, LocalEventBus())  # Leaves a pending planner.request.
            self.position = store.position()
            row = store.db.execute("SELECT topic, payload FROM outbox WHERE topic='planner.request'").fetchone()
        self.request = {"topic": row[0], "payload": json.loads(row[1])}
        self.journal_path = self.directory / "planner" / "transport.db"
        self.journal_path.parent.mkdir()

    def journal(self, *, accepted=True, request=None, result=None, position=None, remembered=None):
        journal = WorkerJournal(self.journal_path)
        try:
            delivery = dict(request or self.request, delivery_id="D-1", lease_token="t" * 32)
            key = journal.bind(delivery)
            journal.save(key, result or sample("proposal.json"))
            if accepted:
                journal.accepted(key, position or self.position)
            if remembered:
                journal.remember_store(*remembered)
        finally:
            journal.close()

    def run_main(self, *arguments):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = restore_check.main(["--db", str(self.database), *arguments])
        return code, output.getvalue(), errors.getvalue()

    def test_an_untouched_database_reports_nothing_missing(self):
        with SQLiteStore(self.database) as store:
            handle(sample("proposal.json"), store, LocalEventBus())
            current = store.position()
        self.journal(position=current, remembered=current)
        code, output, _ = self.run_main("--journal", str(self.journal_path), "--previous-commit", str(current[1]))
        self.assertEqual(code, 0, output)
        self.assertIn("Nothing acknowledged is missing.", output)
        self.assertIn("1 acknowledged, 0 missing, 0 not yet sent", output)

    def test_an_acknowledged_result_with_its_request_pending_again_is_replayed(self):
        later = (self.position[0], self.position[1] + 3)
        self.journal(position=later, remembered=later)
        code, output, _ = self.run_main("--journal", str(self.journal_path), "--json")
        self.assertEqual(code, 1)
        report = json.loads(output)
        (journal,) = report["journals"]
        self.assertEqual((journal["rolled_back"], journal["lost_commits"]), (True, 3))
        (missing,) = journal["missing"]
        self.assertEqual({key: missing[key] for key in ("kind", "action_id", "topic", "redrive")},
                         {"kind": "proposal", "action_id": "ACT-DEMO-001", "topic": "planner.request",
                          "redrive": "replay"})
        self.assertEqual((missing["accepted_generation"], missing["accepted_commit"]), later)
        self.assertTrue(report["may_lose_acknowledged_work"])
        self.assertEqual(journal["journal"], "planner/transport.db")  # Folder and name only, never a full path.
        self.assertNotIn(str(self.directory), output)

    def test_an_acknowledged_result_with_no_pending_request_needs_review(self):
        other = deepcopy(self.request)
        other["payload"]["incident"]["incident_id"] = "INC-ELSEWHERE"
        self.journal(request=other)
        code, output, _ = self.run_main("--journal", str(self.journal_path))
        self.assertEqual(code, 1)
        self.assertIn("review: no pending request asks for this result", output)

    def test_a_different_stored_payload_under_the_same_key_is_flagged(self):
        changed = dict(sample("proposal.json"), rationale="another plan stored under the same key")
        with SQLiteStore(self.database) as store:
            handle(changed, store, LocalEventBus())
        self.journal()
        code, output, _ = self.run_main("--journal", str(self.journal_path), "--json")
        self.assertEqual(code, 1)
        (missing,) = json.loads(output)["journals"][0]["missing"]
        self.assertEqual((missing["redrive"], missing["conflict"]),
                         ("review", "the restored database holds a different message under this key"))

    def test_saved_results_not_yet_acknowledged_are_counted_not_missing(self):
        self.journal(accepted=False)
        code, output, _ = self.run_main("--journal", str(self.journal_path))
        self.assertEqual(code, 0, output)
        self.assertIn("0 acknowledged, 0 missing, 1 not yet sent", output)

    def test_a_previous_counter_ahead_of_the_restored_one_is_a_rollback(self):
        code, output, _ = self.run_main("--previous-commit", str(self.position[1] + 7))
        self.assertEqual(code, 1)
        self.assertIn("It is 7 commits behind the counter noted before the restore.", output)
        code, output, _ = self.run_main("--previous-commit", str(self.position[1]))
        self.assertEqual(code, 0)

    def test_another_generation_is_a_different_database(self):
        code, output, _ = self.run_main("--previous-generation", "f" * 32, "--previous-commit", "1")
        self.assertEqual(code, 1)
        self.assertIn("It is a different database from the one noted before the restore.", output)
        self.journal(remembered=("e" * 32, 1))
        code, output, _ = self.run_main("--journal", str(self.journal_path), "--json")
        self.assertTrue(json.loads(output)["journals"][0]["different_database"])

    def test_a_journal_written_before_p3_06_is_read(self):
        db = sqlite3.connect(self.journal_path)
        db.execute("""CREATE TABLE worker_deliveries (request_key TEXT PRIMARY KEY, topic TEXT NOT NULL,
            payload TEXT NOT NULL, delivery TEXT, result TEXT, report TEXT, accepted INTEGER NOT NULL DEFAULT 0)""")
        db.execute("INSERT INTO worker_deliveries VALUES('k','planner.request',?,NULL,?,NULL,1)",
                   (json.dumps(self.request["payload"]), json.dumps(sample("proposal.json"))))
        db.commit()
        db.close()
        code, output, _ = self.run_main("--journal", str(self.journal_path), "--json")
        self.assertEqual(code, 1)
        journal = json.loads(output)["journals"][0]
        self.assertIsNone(journal["remembered"])
        self.assertEqual([item["redrive"] for item in journal["missing"]], ["replay"])

    def test_a_database_from_before_migration_5_is_reported_without_a_generation(self):
        old = self.directory / "old.db"
        db = sqlite3.connect(old)
        db.executescript((ROOT / "tests/fixtures/coordinator_schema_v0.sql").read_text(encoding="utf-8"))
        db.close()
        code, output, _ = self.run_main("--previous-commit", "5")
        self.assertEqual(code, 1)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(restore_check.main(["--db", str(old)]), 0)
        self.assertIn("The database predates store generations (migration 5)", output.getvalue())

    def test_inputs_are_never_changed_and_an_unreadable_one_exits_2(self):
        self.journal()
        before = self.database.read_bytes(), self.journal_path.read_bytes()
        self.run_main("--journal", str(self.journal_path))
        self.assertEqual((self.database.read_bytes(), self.journal_path.read_bytes()), before)
        code, _, errors = self.run_main("--journal", str(self.directory / "absent.db"))
        self.assertEqual(code, 2)
        self.assertIn("Cannot check the restore: FileNotFoundError", errors)
        self.assertNotIn(str(self.directory), errors)


if __name__ == "__main__":
    unittest.main()
