"""The gates on new work, together: store change (P3.06), clock skew (P3.03), preflight (P3.07).

Each polling pass re-sends saved results first. Only then may it claim, and
only when no Coordinator store change is recorded, the clock is within the
skew bound, and the component's prerequisite (the agent Planner's model
preflight) passes, checked in that order so the preflight never runs on a
pass that would refuse the claim anyway. /readyz is 503 while any of them
fails, and its body names each failing one with a true boolean.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from runtime import worker_service
from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
from runtime.worker_health import PreflightFailed, WorkerHealth
from runtime.worker_http import StoreWatch, WorkerClient, WorkerHTTPServer, handler, serve_until_stopped
from runtime.worker_transport import Delivery
from services.coordinator.store import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]
REASONS = ("clock_skew", "store_changed", "prerequisites_failed")


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / name).read_text(encoding="utf-8"))


def copy_database(source, target):
    with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
        src.backup(dst)
    src.close()
    dst.close()


class Planner:
    def __init__(self):
        self.calls = 0

    def process(self, delivery, lease):
        self.calls += 1
        return deepcopy(sample("proposal.json")), None


class CountingClient(WorkerClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.operations = []

    def post(self, operation, body):
        self.operations.append(operation)
        return super().post(operation, body)


class ClaimGatingTests(unittest.TestCase):
    """A loopback Coordinator, a planner worker with a 2 s skew bound, a store watch and a preflight."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = self.directory / "coordinator.db"
        self.services = {"planner": ("planner", "p" * 40), "observer": ("observer", "o" * 40)}
        stopping = threading.Event()
        with SQLiteStore(self.database) as store:
            Delivery(store)
        server = stack.enter_context(WorkerHTTPServer(("127.0.0.1", 0), handler(
            self.database, self.services, stopping=stopping, max_clock_skew=None)))
        thread = threading.Thread(target=serve_until_stopped, args=(server, stopping))
        thread.start()
        stack.callback(thread.join)
        stack.callback(stopping.set)
        self.url = f"http://127.0.0.1:{server.server_port}"
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=5))(
            sample("incident.json"))
        self.backup = self.directory / "backup.db"
        copy_database(self.database, self.backup)  # Before the planner's result is acknowledged.
        self.offset = [0.0]
        self.client = CountingClient(self.url, "planner", self.services["planner"][1], timeout=5,
                                     clock=lambda: time.time() + self.offset[0], store=StoreWatch())
        self.journal = WorkerJournal(self.directory / "transport.db")
        self.addCleanup(self.journal.close)
        self.adapter = Planner()
        self.prerequisite = Mock()
        self.worker = RemoteWorker(self.client, self.journal, self.adapter, max_clock_skew=2,
                                   before_claim=self.prerequisite)
        self.health = WorkerHealth()
        self.health.configured()
        self.health.prerequisites(True)

    def poll(self):
        """One pass as worker_service runs it, with the preflight recorded as main() records it."""
        prerequisite, health = self.prerequisite, self.health

        def recorded():
            try:
                prerequisite()
            except PreflightFailed:
                health.prerequisites(False)
                raise
            health.prerequisites(True)
        before = len(self.client.operations)
        with patch.object(self.worker, "before_claim", recorded), \
                redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
            worker_service.poll(self.worker, self.health, threading.Event(), 2, once=True)
        return json.loads(output.getvalue().splitlines()[-1]), self.client.operations[before:]

    def readyz(self):
        status, body = self.health.ready()
        return status, sorted(reason for reason in REASONS if body.get(reason) is True)

    def accepted_then_restored(self):
        """Plan and deliver once, then restore the Coordinator database from before that result."""
        status, _ = self.poll()
        self.assertEqual(status, {"status": "accepted", "replay": False})
        self.assertEqual(self.readyz(), (200, []))
        copy_database(self.backup, self.database)

    def test_a_rollback_refuses_new_work_without_running_the_preflight(self):
        self.accepted_then_restored()
        self.prerequisite.side_effect = PreflightFailed("MODEL_UNAVAILABLE")
        self.client.post("clock", {})  # Any reply shows the restore; the clock is in bound.
        calls = self.prerequisite.call_count
        status, operations = self.poll()
        self.assertEqual(status, {"status": "store_changed", "reason": "rolled_back"})
        self.assertEqual(operations, [])  # A journal read only: no claim, no clock exchange.
        self.assertEqual(self.prerequisite.call_count, calls)
        self.assertEqual(self.readyz(), (503, ["store_changed"]))  # The preflight was not run, so not named.

    def test_readyz_names_every_failing_gate_and_clears_each_as_it_recovers(self):
        self.accepted_then_restored()
        # The preflight fails with the clock in bound: it alone is named, and nothing is claimed.
        self.prerequisite.side_effect = PreflightFailed("MODEL_UNAVAILABLE")
        status, operations = self.poll()
        self.assertEqual(status, {"status": "not_ready", "error_code": "MODEL_UNAVAILABLE"})
        self.assertNotIn("claim", operations)
        self.assertEqual(self.readyz(), (503, ["prerequisites_failed"]))
        calls = self.prerequisite.call_count

        # The clock steps out of bound and a reply shows the restore: all three are named.
        self.offset[0] = -5.0
        self.client.post("clock", {})
        status, operations = self.poll()
        self.assertEqual(status["status"], "store_changed")
        self.assertEqual(operations, [])
        self.assertEqual(self.readyz(), (503, ["clock_skew", "prerequisites_failed", "store_changed"]))

        # An operator accepts the restore: the clock is checked next, still before the preflight.
        self.journal.accept_store()
        status, operations = self.poll()
        self.assertEqual(status["status"], "clock_skew")
        self.assertNotIn("claim", operations)
        self.assertEqual(self.readyz(), (503, ["clock_skew", "prerequisites_failed"]))
        self.assertEqual(self.prerequisite.call_count, calls)

        # The clock and the model recover; the restored request gets the saved result, not a new plan.
        self.offset[0] = 0.0
        self.prerequisite.side_effect = None
        for _ in range(10):
            status, _operations = self.poll()
            if status["status"] == "accepted":
                break
        self.assertEqual(status["status"], "accepted")
        self.assertEqual(self.readyz(), (200, []))
        self.assertGreater(self.prerequisite.call_count, calls)
        self.assertEqual(self.adapter.calls, 1)

    def test_a_saved_result_is_resent_first_while_every_gate_fails(self):
        original = self.client.post

        def lose_result(operation, body):
            if operation == "result":
                raise TimeoutError("the result request was lost before the Coordinator read it")
            return original(operation, body)
        with patch.object(self.client, "post", side_effect=lose_result), self.assertRaises(TimeoutError):
            self.worker.once()  # Planned and saved, not acknowledged.
        self.offset[0] = -5.0
        self.client.post("clock", {})  # The clock steps out of bound,
        self.client.store.change = {"reason": "rolled_back", "previous_generation": None,
                                    "previous_commit": 99, "generation": None, "commit": 1}  # a reply shows a restore,
        self.prerequisite.reset_mock()
        self.prerequisite.side_effect = PreflightFailed("MODEL_UNAVAILABLE")  # and the model is down.
        status, operations = self.poll()
        self.assertEqual(status, {"status": "accepted", "replay": False})
        self.assertEqual(operations, ["result"])
        self.assertEqual(self.prerequisite.call_count, 0)
        status, operations = self.poll()
        self.assertEqual(status["status"], "store_changed")
        self.assertEqual((operations, self.prerequisite.call_count, self.adapter.calls), ([], 0, 1))

    def test_a_failing_preflight_backs_off_with_jitter_up_to_the_cap_and_refusals_do_not(self):
        self.prerequisite.side_effect = PreflightFailed("MODEL_UNAVAILABLE")
        stopping, waits = threading.Event(), []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 5:
                stopping.set()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            worker_service.poll(self.worker, self.health, stopping, 2, wait=wait, random=lambda: 1.0)
        self.assertEqual(waits, [4, 8, 15, 15, 15])  # Each failed preflight counts as a failed pass.
        self.assertNotIn("claim", self.client.operations)

        # A refused claim (store change) is an answered pass: the poll interval, no back-off.
        self.client.store.change = {"reason": "rolled_back", "previous_generation": None,
                                    "previous_commit": 99, "generation": None, "commit": 1}
        stopping.clear()
        waits.clear()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            worker_service.poll(self.worker, self.health, stopping, 2, wait=wait, random=lambda: 1.0)
        self.assertEqual(waits, [2, 2, 2, 2, 2])
        self.assertEqual(self.health.ready()[1].get("store_changed"), True)


if __name__ == "__main__":
    unittest.main()
