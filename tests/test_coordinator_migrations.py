"""PRAGMA user_version migrations keep databases from earlier releases working."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from runtime.worker_transport import Delivery
from services.coordinator.core import LocalEventBus, handle
from services.coordinator.read import read_records
from services.coordinator.store import (
    CoordinatorError, MIGRATIONS, SCHEMA_VERSION, SQLiteStore, migrate, schema_version,
)

ROOT = Path(__file__).resolve().parents[1]
# A dump of a Coordinator database written by the code before schema versioning
# (main at d5b2ed9): one incident with a VALIDATING proposal and transport tables.
FIXTURE = Path(__file__).parent / "fixtures/coordinator_schema_v0.sql"


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / name).read_text())


def tables(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class MigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coordinator.db"

    def old_database(self):
        db = sqlite3.connect(self.path)
        db.executescript(FIXTURE.read_text(encoding="utf-8"))
        db.close()

    def version(self):
        db = sqlite3.connect(self.path)
        try:
            return schema_version(db), tables(db)
        finally:
            db.close()

    def test_old_schema_database_is_upgraded_in_place_and_keeps_working(self):
        self.old_database()
        version, before = self.version()
        self.assertEqual(version, 0)
        self.assertNotIn("transitions", before)
        self.assertEqual(read_records(self.path)["transitions"], [])  # The reader never migrates.
        incident, proposal = sample("incident.json"), sample("proposal.json")
        with SQLiteStore(self.path) as store:
            self.assertEqual(schema_version(store.db), SCHEMA_VERSION)
            self.assertEqual(store.incident(incident["incident_id"])["state"], "VALIDATING")
            self.assertEqual(store.action(proposal["action_id"])["proposal"], proposal)
            self.assertEqual(store.transitions(), [])  # History before the journal is not invented.
            self.assertFalse(handle(proposal, store, LocalEventBus()))  # Stored messages still deduplicate.
            delivery = Delivery(store, dependency_requests=True)
            claimed = delivery.claim("verifier", "verifier")["delivery"]
            self.assertEqual((claimed["topic"], claimed["event_id"]), ("dependency.request", 3))
            store.db.execute("UPDATE incidents SET state='REJECTED'")
            store.db.execute("UPDATE actions SET state='REJECTED'")
            retry = dict(proposal, action_id="ACT-DEMO-002")
            self.assertTrue(handle(retry, store, LocalEventBus()))
            self.assertEqual([(t["from_state"], t["to_state"]) for t in store.transitions()],
                             [("REJECTED", "VALIDATING")])
        version, after = self.version()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertLessEqual(before | {"transitions"}, after)
        self.assertEqual(len(read_records(self.path)["transitions"]), 1)

    def test_new_database_gets_every_migration(self):
        with SQLiteStore(self.path):
            pass
        version, names = self.version()
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertLessEqual({"incidents", "actions", "messages", "outbox", "transitions"}, names)

    def test_reopening_is_idempotent_and_concurrent_stores_share_one_version(self):
        self.old_database()
        with SQLiteStore(self.path) as first, SQLiteStore(self.path) as second:
            handle(sample("incident.json") | {"incident_id": "INC-2"}, first, LocalEventBus())
            self.assertEqual(len(second.transitions()), 1)
        with SQLiteStore(self.path) as store:
            self.assertEqual(len(store.transitions()), 1)
        self.assertEqual(self.version()[0], SCHEMA_VERSION)

    def test_a_database_left_at_any_earlier_version_reaches_the_newest(self):
        # Trace context (3), Coordinator sequence and worker clocks (4), then the
        # store generation, commit counter and messages(kind, action_id) index (5).
        # Version 0 is the unversioned schema in FIXTURE, upgraded above.
        for stopped in range(1, SCHEMA_VERSION):
            with self.subTest(version=stopped):
                self.path.unlink(missing_ok=True)
                db = sqlite3.connect(self.path, isolation_level=None)
                try:
                    migrate(db, MIGRATIONS[:stopped])
                finally:
                    db.close()
                self.assertEqual(self.version()[0], stopped)
                with SQLiteStore(self.path) as store:
                    self.assertEqual(schema_version(store.db), SCHEMA_VERSION)
                    columns = {row[1] for row in store.db.execute("PRAGMA table_info(messages)")}
                    self.assertLessEqual({"seq", "answers", "high_water", "producer", "kind", "action_id"}, columns)
                    self.assertIn("trace_context", {row[1] for row in store.db.execute("PRAGMA table_info(outbox)")})
                    self.assertLessEqual({"transitions", "worker_clocks", "store_meta"}, tables(store.db))
                    generation, counter = store.position()
                    self.assertRegex(generation, r"^[0-9a-f]{32}$")
                    self.assertTrue(handle(sample("incident.json"), store, LocalEventBus()))
                    self.assertGreater(store.position()[1], counter)
                    self.assertEqual(store.db.execute("SELECT kind FROM messages").fetchone()[0], "incident")

    def test_migrations_are_numbered_in_order(self):
        numbers = [number for number, _ in MIGRATIONS]
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))
        self.assertEqual(SCHEMA_VERSION, numbers[-1])

    def test_newer_schema_is_refused_and_the_file_is_released(self):
        with SQLiteStore(self.path):
            pass
        db = sqlite3.connect(self.path)
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        db.close()
        with self.assertRaisesRegex(CoordinatorError, "newer"):
            SQLiteStore(self.path)
        self.path.unlink()  # The refused store closed its connection.

    def test_a_failing_migration_rolls_back_with_its_version(self):
        with SQLiteStore(self.path):
            pass
        broken = MIGRATIONS + ((SCHEMA_VERSION + 1, ("CREATE TABLE partial (a)", "NOT VALID SQL")),)
        db = sqlite3.connect(self.path, isolation_level=None)
        try:
            with self.assertRaises(sqlite3.Error):
                migrate(db, broken)
            self.assertFalse(db.in_transaction)
            self.assertEqual(schema_version(db), SCHEMA_VERSION)
            self.assertNotIn("partial", tables(db))
            fixed = MIGRATIONS + ((SCHEMA_VERSION + 1, ("CREATE TABLE partial (a)",)),)
            self.assertEqual(migrate(db, fixed), SCHEMA_VERSION + 1)
            self.assertIn("partial", tables(db))
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
