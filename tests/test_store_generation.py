"""Restore detection (P3.06): store generation, commit counter, and workers that stop claiming.

The Coordinator database gets a random generation once and a commit counter
that grows with every transaction that changes a row. Every worker reply
carries both. A worker that sees another generation, or a lower counter than
one it saw before sending, stops claiming new work until an operator accepts
the restore, and keeps every saved result.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest

from runtime import worker_service
from runtime.coordinator_server import servers
from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
from runtime.worker_health import WorkerHealth
from runtime.worker_http import StoreWatch, WorkerClient, parse_store_position
from runtime.worker_transport import Delivery
from scripts import coordinator_restore_check
from services.coordinator.core import LocalEventBus, handle
from services.coordinator.store import MIGRATIONS, SCHEMA_VERSION, SQLiteStore, migrate, schema_version
from shared import log

ROOT = Path(__file__).resolve().parents[1]
GENERATION = "0123456789abcdef0123456789abcdef"


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / name).read_text(encoding="utf-8"))


def copy_database(source, target):
    """Copy a SQLite database with the backup API, as an operator's backup or restore would."""
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    src.close()
    dst.close()


class Planner:
    """A planner stand-in that returns the sample proposal and counts its calls."""
    def __init__(self):
        self.calls = 0

    def process(self, delivery, lease):
        self.calls += 1
        return deepcopy(sample("proposal.json")), None


class CountingClient(WorkerClient):
    """A real client that counts the operations it sends."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.operations = []

    def post(self, operation, body):
        self.operations.append(operation)
        return super().post(operation, body)


class BackupBeforeResultClient(CountingClient):
    """A real client that backs up the Coordinator database just before it sends its first result."""
    def __init__(self, *args, database, backup, **kwargs):
        super().__init__(*args, **kwargs)
        self.database, self.backup = database, backup

    def post(self, operation, body):
        if operation == "result" and not self.backup.exists():
            copy_database(self.database, self.backup)
        return super().post(operation, body)


class StoreGenerationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)

    def test_a_new_database_gets_a_generation_and_reopening_keeps_it(self):
        path = self.directory / "coordinator.db"
        with SQLiteStore(path) as store:
            generation, commit = store.position()
        self.assertRegex(generation, r"^[0-9a-f]{32}$")
        self.assertEqual(commit, 0)
        with SQLiteStore(path) as store:
            self.assertEqual(store.position(), (generation, 0))

    def test_another_database_differs_and_a_copied_file_keeps_its_generation(self):
        first, second = self.directory / "a.db", self.directory / "b.db"
        with SQLiteStore(first) as store:
            handle(sample("incident.json"), store, LocalEventBus())
            original = store.position()
        with SQLiteStore(second) as store:
            self.assertNotEqual(store.position()[0], original[0])
        copy = self.directory / "copy.db"
        shutil.copyfile(first, copy)
        with SQLiteStore(copy) as store:
            self.assertEqual(store.position(), original)

    def test_the_counter_grows_once_per_changing_transaction_only(self):
        with SQLiteStore(self.directory / "coordinator.db") as store:
            handle(sample("incident.json"), store, LocalEventBus())
            self.assertEqual(store.position()[1], 1)
            with store.transaction():
                store.incident("INC-DEMO-001")  # Reads only.
            self.assertEqual(store.position()[1], 1)
            with self.assertRaises(RuntimeError), store.transaction():
                store.enqueue("planner.request", {"x": 1})
                raise RuntimeError("rolled back")
            self.assertEqual(store.position()[1], 1)
            delivery = Delivery(store)
            self.assertEqual(delivery.claim("planner", "planner")["delivery"]["topic"], "planner.request")
            self.assertEqual(store.position()[1], 2)  # The claim's receipt is a change.
            self.assertFalse(handle(sample("incident.json"), store, LocalEventBus()))
            self.assertEqual(store.position()[1], 2)  # An exact replay writes nothing.


class MessageIndexMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coordinator.db"

    def version_4_database(self):
        db = sqlite3.connect(self.path, isolation_level=None)
        migrate(db, MIGRATIONS[:4])
        rows = [("k1", sample("incident.json")), ("k2", sample("proposal.json")),
                ("k3", sample("dependency-result.json"))]
        for seq, (key, message) in enumerate(rows, 1):
            db.execute("INSERT INTO messages(event_key,payload,seq) VALUES(?,?,?)", (key, json.dumps(message), seq))
        db.execute("INSERT INTO messages(event_key,payload,seq) VALUES('k4','not json',4)")
        db.close()

    def indexes(self, db):
        return [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='messages' AND name='messages_kind_action'")]

    def test_a_version_4_database_is_backfilled_indexed_and_reopening_is_idempotent(self):
        self.version_4_database()
        for _ in range(2):
            with SQLiteStore(self.path) as store:
                self.assertEqual(schema_version(store.db), SCHEMA_VERSION)
                self.assertEqual(self.indexes(store.db), ["messages_kind_action"])
                rows = [tuple(row) for row in store.db.execute("SELECT event_key,kind,action_id FROM messages ORDER BY seq")]
                self.assertEqual(rows, [("k1", "incident", None), ("k2", "proposal", "ACT-DEMO-001"),
                                        ("k3", "dependency_result", "ACT-DEMO-001"), ("k4", None, None)])
                meta = store.accepted_meta("dependency_result", "ACT-DEMO-001")
                self.assertEqual((meta["seq"], meta["message"]), (3, sample("dependency-result.json")))
                self.assertIsNone(store.accepted_meta("dependency_result", "ACT-OTHER"))
                self.assertRegex(store.position()[0], r"^[0-9a-f]{32}$")
        with SQLiteStore(self.path) as store:
            generation = store.position()[0]
            # The index and meta-row statements of migration 5 can run again without effect.
            for statement in MIGRATIONS[4][1]:
                if "ALTER TABLE" not in statement:
                    store.db.execute(statement)
            self.assertEqual(self.indexes(store.db), ["messages_kind_action"])
            self.assertEqual(store.position()[0], generation)

    def test_the_lookup_uses_the_index(self):
        with SQLiteStore(self.path) as store:
            plan = " ".join(row[3] for row in store.db.execute(
                "EXPLAIN QUERY PLAN SELECT payload FROM messages WHERE kind=? AND action_id IS ? "
                "ORDER BY seq DESC LIMIT 1", ("dependency_result", "ACT-DEMO-001")))
        self.assertIn("messages_kind_action", plan)

    def test_new_messages_record_their_kind_and_action(self):
        with SQLiteStore(self.path) as store:
            handle(sample("incident.json"), store, LocalEventBus())
            handle(sample("proposal.json"), store, LocalEventBus())
            rows = [tuple(row) for row in store.db.execute("SELECT kind,action_id FROM messages ORDER BY seq")]
        self.assertEqual(rows, [("incident", None), ("proposal", "ACT-DEMO-001")])


class StoreWatchTests(unittest.TestCase):
    def test_first_reply_is_adopted_and_growth_is_not_a_change(self):
        watch = StoreWatch()
        self.assertIsNone(watch.observe(watch.floor(), GENERATION, "5"))
        self.assertIsNone(watch.observe(watch.floor(), GENERATION, "9"))
        self.assertEqual((watch.generation, watch.highest, watch.change), (GENERATION, 9, None))

    def test_a_lower_counter_after_a_higher_one_was_seen_is_a_rollback(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "9")
        change = watch.observe(watch.floor(), GENERATION, "4")
        self.assertEqual(change, {"reason": "rolled_back", "previous_generation": GENERATION,
                                  "previous_commit": 9, "generation": GENERATION, "commit": 4})
        self.assertIsNone(watch.observe(watch.floor(), GENERATION, "3"))  # Reported once.
        self.assertEqual(watch.change, change)

    def test_replies_to_requests_in_flight_together_may_arrive_in_either_order(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "5")
        renew_floor, claim_floor = watch.floor(), watch.floor()  # Both sent before either reply.
        self.assertIsNone(watch.observe(claim_floor, GENERATION, "7"))
        self.assertIsNone(watch.observe(renew_floor, GENERATION, "6"))  # Read earlier, answered later.
        self.assertIsNone(watch.change)

    def test_another_generation_is_a_change_and_nothing_is_adopted(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "9")
        change = watch.observe(watch.floor(), "f" * 32, "100")
        self.assertEqual(change["reason"], "generation_changed")
        self.assertEqual((watch.generation, watch.highest), (GENERATION, 9))
        watch.reset()
        self.assertIsNone(watch.observe(watch.floor(), "f" * 32, "100"))
        self.assertEqual(watch.generation, "f" * 32)

    def test_the_counter_before_the_request_is_compared_so_its_own_commit_hides_nothing(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "5")
        # A restore lost one commit (5 -> 4); this request's own commit brings it back to 5.
        change = watch.observe(watch.floor(), GENERATION, "5", "4")
        self.assertEqual(change, {"reason": "rolled_back", "previous_generation": GENERATION,
                                  "previous_commit": 5, "generation": GENERATION, "commit": 4})

    def test_a_before_value_at_the_floor_is_not_a_change(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "5")
        self.assertIsNone(watch.observe(watch.floor(), GENERATION, "6", "5"))
        self.assertIsNone(watch.observe(watch.floor(), GENERATION, "6", "6"))  # A read-only request.
        for malformed in ("x", "-1", "", None):
            with self.subTest(before=malformed):
                self.assertIsNone(watch.observe(watch.floor(), GENERATION, "7", malformed))
        self.assertEqual((watch.highest, watch.change), (7, None))

    def test_the_highest_counter_only_grows(self):
        watch = StoreWatch()
        watch.observe(None, GENERATION, "5")
        shared = watch.floor()  # A claim and a renewal sent together.
        self.assertIsNone(watch.observe(shared, GENERATION, "7"))
        self.assertIsNone(watch.observe(shared, GENERATION, "6"))  # The renewal answered later.
        self.assertEqual(watch.highest, 7)
        self.assertIsNone(watch.observe(5, GENERATION, "6"))  # A floor read before 7 arrived.
        self.assertEqual(watch.highest, 7)
        change = watch.observe(watch.floor(), GENERATION, "6")
        self.assertEqual((change["reason"], change["previous_commit"]), ("rolled_back", 7))

    def test_the_latest_reply_is_kept_per_thread(self):
        watch = StoreWatch()
        self.assertIsNone(watch.last_seen())
        watch.observe(None, GENERATION, "5")  # This thread's result reply.
        renewal = threading.Thread(target=watch.observe, args=(watch.floor(), GENERATION, "9"))
        renewal.start()  # A lease renewal answered afterwards on another thread.
        renewal.join()
        self.assertEqual(watch.last_seen(), (GENERATION, 5))
        self.assertEqual(watch.highest, 9)

    def test_missing_or_malformed_headers_are_ignored(self):
        watch = StoreWatch()
        for generation, commit in ((None, None), ("XYZ", "1"), (GENERATION, "-1"), (GENERATION, "1e3"),
                                   (GENERATION, "١٢"), (GENERATION.upper(), "1"), (GENERATION, "9" * 19)):
            with self.subTest(generation=generation, commit=commit):
                self.assertIsNone(parse_store_position(generation, commit))
                self.assertIsNone(watch.observe(None, generation, commit))
        self.assertIsNone(watch.generation)


class JournalPositionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.journal = WorkerJournal(Path(directory.name) / "transport.db")
        self.addCleanup(self.journal.close)

    def bound(self):
        key = self.journal.bind({"delivery_id": "D-1", "lease_token": "t" * 32, "topic": "planner.request",
                                 "payload": {"incident": {"incident_id": "INC-1"}}})
        self.journal.save(key, sample("proposal.json"))
        return key

    def test_the_remembered_counter_keeps_the_larger_value_for_one_generation(self):
        self.journal.remember_store(GENERATION, 9)
        self.journal.remember_store(GENERATION, 4)
        self.assertEqual(self.journal.store_position(), (GENERATION, 9, None))
        self.journal.remember_store("f" * 32, 2)  # Another generation replaces it.
        self.assertEqual(self.journal.store_position(), ("f" * 32, 2, None))

    def test_acknowledging_a_result_and_remembering_its_position_commit_together(self):
        key = self.bound()
        self.journal.db.execute("""CREATE TRIGGER refuse BEFORE INSERT ON coordinator_store
            BEGIN SELECT RAISE(ABORT, 'disk full'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.journal.accepted(key, (GENERATION, 7), remember=True)
        (row,) = self.journal.db.execute("SELECT accepted, accepted_commit FROM worker_deliveries")
        self.assertEqual(tuple(row), (0, None))  # Neither was written.
        self.journal.db.execute("DROP TRIGGER refuse")
        self.journal.accepted(key, (GENERATION, 7), remember=True)
        (row,) = self.journal.db.execute("SELECT accepted, accepted_commit FROM worker_deliveries")
        self.assertEqual(tuple(row), (1, 7))
        self.assertEqual(self.journal.store_position(), (GENERATION, 7, None))


class ReadinessTests(unittest.TestCase):
    def test_a_store_change_makes_the_worker_unready_until_cleared(self):
        health = WorkerHealth(clock=lambda: 5.0)
        health.configured()
        health.contacted()
        self.assertEqual(health.ready(), (200, {"ready": True, "contact_age_seconds": 0.0}))
        health.store_checked(False)
        status, body = health.ready()
        self.assertEqual((status, body["ready"], body["store_changed"]), (503, False, True))
        health.store_checked(True)
        self.assertEqual(health.ready()[0], 200)

    def test_a_polling_pass_that_finds_a_store_change_turns_readyz_unready(self):
        class Worker:
            current = {}

            def __init__(self, changed):
                self.changed = changed

            def once(self):
                return {"status": "store_changed", "reason": "rolled_back"} if self.changed else {"status": "idle"}

            def clock_ready(self):
                return True

            def store_ready(self):
                return not self.changed

        health = WorkerHealth(clock=lambda: 5.0)
        health.configured()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(worker_service.poll(Worker(True), health, threading.Event(), 2, once=True), 0)
        status, body = health.ready()
        self.assertEqual((status, body.get("store_changed")), (503, True))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            worker_service.poll(Worker(False), health, threading.Event(), 2, once=True)
        self.assertEqual(health.ready()[0], 200)


class LoopbackRestoreTests(unittest.TestCase):
    """A Coordinator over loopback HTTP whose database is restored from a backup underneath it."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.directory / "coordinator.db"
        incident = sample("incident.json")
        self.services = {role: (role, role[0] * 40) for role in ("observer", "planner", "guard", "executor", "verifier")}
        approvals = {"operator": ("operator", "a" * 40)}
        permissions = {"operator": [{"target": incident["target"], "operations": ["read"]}]}
        workers, _approvals, _stopping, _threads = self.stack.enter_context(servers(
            self.database, self.services, approvals, permissions, port=0, approval_port=0, lease_seconds=5))
        self.url = f"http://127.0.0.1:{workers.server_port}"
        self.log = io.StringIO()
        self.stack.enter_context(log.configured("test", level="DEBUG", stream=self.log))

    def client(self, role, cls=WorkerClient):
        return cls(self.url, role, self.services[role][1], timeout=5)

    def planner(self, adapter, client=None):
        journal = WorkerJournal(self.directory / "planner-transport.db")
        self.stack.callback(journal.close)
        return RemoteWorker(client or self.client("planner", CountingClient), journal, adapter, renewal_interval=0.5)

    def state(self):
        with SQLiteStore(self.database) as store:
            return store.incident("INC-DEMO-001")["state"]

    def events(self, name):
        return [record for record in map(json.loads, self.log.getvalue().splitlines()) if record["event"] == name]

    def test_a_rolled_back_database_stops_claiming_keeps_saved_results_and_resumes_after_acceptance(self):
        RemoteIngress(self.client("observer"))(sample("incident.json"))
        backup = self.directory / "backup.db"
        copy_database(self.database, backup)  # Taken before the planner's result was acknowledged.
        adapter = Planner()
        worker = self.planner(adapter)
        self.assertEqual(worker.once()["status"], "accepted")
        self.assertEqual(self.state(), "VALIDATING")
        generation, remembered, change = worker.journal.store_position()
        self.assertIsNone(change)
        (row,) = worker.journal.db.execute("SELECT accepted, accepted_generation, accepted_commit FROM worker_deliveries")
        self.assertEqual(tuple(row), (1, generation, remembered))

        copy_database(backup, self.database)  # The restore: acknowledged work is gone.
        self.assertEqual(self.state(), "OPEN")
        status = worker.once()
        self.assertEqual(status, {"status": "store_changed", "reason": "rolled_back"})
        self.assertEqual(worker.client.operations[-2:], ["claim", "retry"])  # The reclaimed request went back.
        self.assertFalse(worker.store_ready())
        self.assertEqual(adapter.calls, 1)
        (logged,) = self.events("worker.store_rollback_detected")
        self.assertEqual((logged["level"], logged["previous_commit"], logged["store_generation"]),
                         ("error", remembered, generation))
        self.assertLess(logged["commit_counter"], remembered)

        # It stays stopped, and keeps its saved result, across passes and a restart.
        before = len(worker.client.operations)
        self.assertEqual(worker.once()["status"], "store_changed")
        self.assertNotIn("claim", worker.client.operations[before:])
        restarted = self.planner(adapter)
        self.assertFalse(restarted.store_ready())
        self.assertEqual(restarted.once()["status"], "store_changed")
        self.assertEqual(restarted.client.operations, [])
        (saved,) = worker.journal.db.execute("SELECT result, accepted FROM worker_deliveries")
        self.assertEqual((json.loads(saved[0]), saved[1]), (sample("proposal.json"), 1))
        self.assertEqual(len(self.events("worker.store_rollback_detected")), 1)

        # The restore check finds the lost result and how to re-drive it.
        report = coordinator_restore_check.check(self.database, [self.directory / "planner-transport.db"])
        self.assertTrue(report["may_lose_acknowledged_work"])
        (journal,) = report["journals"]
        self.assertTrue(journal["rolled_back"])
        self.assertEqual([(item["kind"], item["redrive"]) for item in journal["missing"]], [("proposal", "replay")])

        # An operator accepts the restore; the worker re-claims and resends its saved result.
        config = self.directory / "planner.json"
        config.write_text(json.dumps({"identity": "planner", "state_directory": str(self.directory / "state")}))
        state = self.directory / "state"
        state.mkdir()
        shutil.copyfile(self.directory / "planner-transport.db", state / "transport.db")  # Prove the CLI edits a journal.
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            self.assertEqual(worker_service.main(["--config", str(config), "--accept-store"]), 0)
        self.assertEqual(json.loads(output.getvalue()), {"status": "store_accepted", "reason": "rolled_back"})
        cleared = WorkerJournal(state / "transport.db")
        self.assertEqual(cleared.store_position(), (None, None, None))
        cleared.close()
        self.assertEqual(restarted.journal.accept_store()["reason"], "rolled_back")
        time.sleep(1.2)  # The handed-back request is offered again after its 1 s retry delay.
        self.assertEqual(restarted.once(), {"status": "accepted", "replay": False})
        self.assertTrue(restarted.store_ready())
        self.assertEqual(adapter.calls, 1)  # Resent from the journal, not planned again.
        self.assertEqual(self.state(), "VALIDATING")
        self.assertEqual(len(self.events("worker.store_change_accepted")), 1)
        self.assertFalse(coordinator_restore_check.check(
            self.database, [self.directory / "planner-transport.db"])["may_lose_acknowledged_work"])

    def test_a_backup_missing_only_the_last_acknowledged_result_is_detected(self):
        RemoteIngress(self.client("observer"))(sample("incident.json"))
        backup = self.directory / "backup.db"
        adapter = Planner()
        worker = self.planner(adapter, BackupBeforeResultClient(
            self.url, "planner", self.services["planner"][1], timeout=5, database=self.database, backup=backup))
        self.assertEqual(worker.once()["status"], "accepted")
        generation, remembered, _change = worker.journal.store_position()
        with SQLiteStore(backup) as store:
            self.assertEqual(store.position(), (generation, remembered - 1))  # A gap of exactly one commit.

        copy_database(backup, self.database)
        self.assertEqual(self.state(), "OPEN")
        # The claim commits too (the worker's clock report), which brings the
        # counter back to the remembered value; the counter read before the
        # claim's own changes still shows the restore.
        self.assertEqual(worker.once(), {"status": "store_changed", "reason": "rolled_back"})
        self.assertFalse(worker.store_ready())
        with SQLiteStore(self.database) as store:
            self.assertEqual(store.position(), (generation, remembered))
        (logged,) = self.events("worker.store_rollback_detected")
        self.assertEqual((logged["previous_commit"], logged["commit_counter"]), (remembered, remembered - 1))
        self.assertEqual(adapter.calls, 1)

    def test_a_replaced_database_is_a_generation_change(self):
        RemoteIngress(self.client("observer"))(sample("incident.json"))
        adapter = Planner()
        worker = self.planner(adapter)
        self.assertEqual(worker.once()["status"], "accepted")
        fresh = self.directory / "fresh.db"
        with SQLiteStore(fresh) as store:
            handle(sample("incident.json"), store, LocalEventBus())
            for _ in range(5):  # A higher counter does not hide another generation.
                with store.transaction():
                    store.enqueue("audit.note", {})
        copy_database(fresh, self.database)
        self.assertEqual(worker.once(), {"status": "store_changed", "reason": "generation_changed"})
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(len(self.events("worker.store_generation_changed")), 1)
        self.assertEqual(worker.journal.store_position()[2]["reason"], "generation_changed")

    def test_saved_unacknowledged_results_are_still_sent_after_a_change(self):
        RemoteIngress(self.client("observer"))(sample("incident.json"))
        adapter = Planner()
        worker = self.planner(adapter)
        self.client("planner").post("clock", {})  # Unrelated traffic before.
        worker.client.post("clock", {})  # The worker adopts the current database.
        original = worker._send

        def lost(row):
            raise TimeoutError("response lost")
        worker._send = lost
        with self.assertRaises(TimeoutError):
            worker.once()  # Claimed and processed; the result is saved but not acknowledged.
        worker._send = original
        worker.client.store.change = {"reason": "rolled_back", "previous_generation": GENERATION,
                                      "previous_commit": 99, "generation": GENERATION, "commit": 1}
        self.assertEqual(worker.once(), {"status": "accepted", "replay": False})  # Sent before the store check.
        self.assertEqual(self.state(), "VALIDATING")
        self.assertEqual(worker.once()["status"], "store_changed")
        self.assertEqual(adapter.calls, 1)


if __name__ == "__main__":
    unittest.main()
