"""Worker back-off after failed passes: capped at 15 s with full jitter (P3.06).

The loopback test stops the Coordinator, lets a real worker fail against the
closed port until its back-off reaches the cap, starts the Coordinator again
on the same port, and checks the worker's next pass reaches it. The waits are
recorded, not slept, so the test takes seconds.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest

from runtime import worker_service
from runtime.coordinator_server import servers
from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
from runtime.worker_health import WorkerHealth
from runtime.worker_http import WorkerClient

ROOT = Path(__file__).resolve().parents[1]


class BackoffTests(unittest.TestCase):
    def test_a_pass_that_reached_the_coordinator_waits_the_poll_interval(self):
        self.assertEqual(worker_service.backoff_seconds(2, 0, lambda: 0.5), 2)

    def test_failures_double_the_ceiling_up_to_the_15_s_cap(self):
        ceilings = [worker_service.backoff_seconds(2, failures, lambda: 1.0) for failures in range(1, 9)]
        self.assertEqual(ceilings, [4, 8, 15, 15, 15, 15, 15, 15])
        self.assertEqual(worker_service.BACKOFF_CAP_SECONDS, 15)

    def test_the_wait_is_drawn_uniformly_below_the_ceiling(self):
        self.assertEqual(worker_service.backoff_seconds(2, 2, lambda: 0.0), 0.0)
        self.assertEqual(worker_service.backoff_seconds(2, 2, lambda: 0.25), 2.0)
        for poll in (0.2, 2, 30, 60):
            for failures in range(1, 12):
                with self.subTest(poll=poll, failures=failures):
                    self.assertLessEqual(worker_service.backoff_seconds(poll, failures), 15)


class LoopbackRestartBackoffTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        incident = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text(encoding="utf-8"))
        self.services = {role: (role, role[0] * 40) for role in ("observer", "planner")}
        self.arguments = (self.directory / "coordinator.db", self.services, {"operator": ("operator", "a" * 40)},
                          {"operator": [{"target": incident["target"], "operations": ["read"]}]})
        self.incident = incident

    def coordinator(self, port=0):
        stack = ExitStack()
        workers, _approvals, _stopping, _threads = stack.enter_context(servers(
            *self.arguments, port=port, approval_port=0, lease_seconds=5))
        return stack, workers.server_port

    def test_a_worker_reaches_a_restarted_coordinator_within_the_cap(self):
        first, port = self.coordinator()
        url = f"http://127.0.0.1:{port}"
        RemoteIngress(WorkerClient(url, "observer", self.services["observer"][1], timeout=5))(self.incident)
        first.close()  # The Coordinator stops.

        class Planner:
            calls = 0

            def process(self, delivery, lease):
                Planner.calls += 1
                return None  # Hand the request back; the pass still reached the Coordinator.

        journal = WorkerJournal(self.directory / "transport.db")
        self.stack.callback(journal.close)
        worker = RemoteWorker(WorkerClient(url, "planner", self.services["planner"][1], timeout=5),
                              journal, Planner(), renewal_interval=0.5)
        health, stopping = WorkerHealth(), threading.Event()
        health.configured()
        statuses, waits = [], []
        once = worker.once

        def counted():
            try:
                status = once()
            except Exception:
                statuses.append("retry")
                raise
            statuses.append(status["status"])
            return status
        worker.once = counted

        def wait(seconds):
            waits.append(seconds)
            if statuses[-1] != "retry":
                stopping.set()  # The worker reached the Coordinator again.
            elif len(waits) == 4:  # Four failures: the ceiling has reached the cap.
                restarted, _ = self.coordinator(port)
                self.stack.callback(restarted.close)

        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = worker_service.poll(worker, health, stopping, 2, wait=wait, random=lambda: 1.0)
        self.assertEqual(code, 0)
        self.assertEqual(statuses, ["retry", "retry", "retry", "retry", "pending"])
        self.assertEqual(waits[:4], [4, 8, 15, 15])  # The fourth wait would have been 32 s before P3.06.
        self.assertLessEqual(max(waits), worker_service.BACKOFF_CAP_SECONDS)
        self.assertEqual(waits[4], 2)  # Back to the poll interval after a pass that reached it.
        self.assertEqual(Planner.calls, 1)
        self.assertEqual(health.ready()[0], 200)


if __name__ == "__main__":
    unittest.main()
