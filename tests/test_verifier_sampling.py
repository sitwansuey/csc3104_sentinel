"""The remote Verifier samples within one pass, at the policy's cadence (L-VERIFIER-RETRY-BACKOFF).

A simulated clock drives the real Coordinator delivery layer (Delivery, its
retry back-off and lease fencing), RemoteWorker and VerifierAdapter in one
process, under the kind bundle's health policy. The lease's renewal thread is
replaced by renewals on each simulated sleep, so no test waits in real time.

Before the fix one pass took one reading and gave the delivery back, so the
Coordinator's doubling back-off spaced the readings: at be6e636 (S2 r2) a
target healthy from about +40 s after the rollback was never confirmed, the
Verifier ended FAILED and its command was delivered 8 times (MAX_ATTEMPTS 6).
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import UTC, datetime
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from experiments.distributed.invariants import MAX_ATTEMPTS
from experiments.distributed.kind_bundle import HEALTH as KIND_HEALTH
from experiments.validation.demo_offline_flow import OfflineFlow
from runtime.remote_adapters import VerifierAdapter
from runtime.remote_worker import RemoteWorker, WorkerJournal
from runtime.service_identity import ProtocolError
from runtime.worker_transport import Delivery
from services.verifier.core import reading_status
from services.verifier.store import MIN_SAMPLE_INTERVAL_SECONDS, VerificationStore, sampling_interval
from shared import log

TERMINAL = {"RECOVERED", "FAILED", "NEEDS_REVIEW"}
POLL_SECONDS = 0.2  # The topology's polling interval.
LEASE_SECONDS = 4  # The kind runs' lease (summary.json lease_seconds at be6e636).


class VerifierCrash(BaseException):
    """Stands for the process dying: RemoteWorker hands nothing back for it."""


class SimLab:
    """One Coordinator, one remote Verifier and a simulated clock."""

    def __init__(self, test, *, healthy_from=40.0, incomplete=False):
        self.test, self.healthy_from, self.incomplete = test, healthy_from, incomplete
        self.stack = ExitStack()
        test.addCleanup(self.stack.close)
        self.directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.f = OfflineFlow(self.directory)
        self.stack.callback(self.f.close)
        self.f.prepare()
        self.f.authorize()
        self.f.execute()
        test.assertEqual(self.state(), "VERIFYING")
        self.action_id = self.f.store.incident(self.f.incident["incident_id"])["active_action_id"]
        self.execution = self.f.store.action(self.action_id)["execution_result"]
        self.template = self.f.store.action(self.action_id)["proposal"]["parameters"]["template_sha256"]
        self.t0 = self.t = self.f.now.timestamp()  # The execution's recorded_at.
        self.delivery = Delivery(self.f.store, clock=lambda: self.t, lease_seconds=LEASE_SECONDS)
        self.leases, self.calls, self.crash_at = [], [], None
        self.policy = deepcopy(self.f.policy)
        self.policy["health"] = deepcopy(KIND_HEALTH)

    # Simulated time ---------------------------------------------------------
    def now(self):
        return datetime.fromtimestamp(self.t, UTC)

    def advance(self, seconds):
        self.t += seconds
        self.f.now = self.now()

    def sleep(self, seconds):
        """Time passes; each held lease is renewed, as Lease's thread renews it every second or so."""
        self.advance(seconds)
        for lease in list(self.leases):
            lease.renew_in_background()

    def after(self):
        return round(self.t - self.t0, 3)

    # Collaborators -------------------------------------------------------
    def reading(self, _execution):
        after = self.t - self.t0
        if self.crash_at is not None and after >= self.crash_at:
            self.crash_at = None
            raise VerifierCrash()
        healthy = after >= self.healthy_from
        return [{"at": self.now().isoformat().replace("+00:00", "Z"), "service": self.execution["target"]["name"],
                 "target": deepcopy(self.execution["target"]), "protocol": "http",
                 "http_status": 200 if healthy else 503, "ready_replicas": int(healthy), "desired_replicas": 1,
                 "available_replicas": int(healthy), "dependencies": {}, "template_sha256": self.template,
                 "configuration_stable": not self.incomplete}]

    def journal(self):
        store = VerificationStore(self.f.path / "verifier.db")
        self.stack.callback(store.close)
        return store

    def adapter(self, journal=None):
        adapter = VerifierAdapter(journal or self.f.verification, policy_reader=lambda _: deepcopy(self.policy),
                                  sample_reader=self.reading, clock=self.now)
        # Set after construction, so the same test runs against the one-reading pass it replaces.
        adapter.sleep = self.sleep
        return adapter

    def worker(self, adapter=None):
        transport = WorkerJournal(self.f.path / "verifier-transport.db")
        self.stack.callback(transport.close)
        return RemoteWorker(SimClient(self), transport, adapter or self.adapter(), renewal_interval=1)

    def lease_class(self):
        lab = self

        class SimLease:
            """Lease without a thread: ensure_current() as Lease, background renewals on each sleep."""

            def __init__(self, client, delivery, *, interval=5, clock=None):
                self.client, self.error = client, None
                self.body = {key: delivery[key] for key in ("delivery_id", "lease_token")}

            def ensure_current(self):
                if self.error is not None:
                    raise self.error
                return self.client.post("renew", self.body)

            def renew_in_background(self):
                if self.error is None:
                    try:
                        self.client.post("renew", self.body)
                    except ProtocolError as error:
                        self.error = error

            def __enter__(self):
                self.ensure_current()
                lab.leases.append(self)
                return self

            def __exit__(self, *_args):
                lab.leases.remove(self)
        return SimLease

    # Driving and reading ---------------------------------------------------
    def run(self, worker, *, until=300):
        """Poll as worker_service does until the incident leaves VERIFYING or until seconds after the write."""
        with patch("runtime.remote_worker.Lease", self.lease_class()):
            while self.state() not in TERMINAL and self.after() < until:
                worker.once()
                self.advance(POLL_SECONDS)
        return self.state()

    def state(self):
        return self.f.store.incident(self.f.incident["incident_id"])["state"]

    def attempts(self):
        return self.f.store.db.execute("""SELECT r.attempts FROM transport_receipts r JOIN outbox o
            ON o.id=r.event_id WHERE o.topic='verifier.request'""").fetchone()[0]

    def readings(self, store=None):
        """(seconds after the write, status) for each saved reading."""
        job = (store or self.f.verification).get(self.action_id)
        return [(round(datetime.fromisoformat(row["at"].replace("Z", "+00:00")).timestamp() - self.t0, 3),
                 reading_status(job["execution"], row, job["policy"])) for row in job["samples"]]


class SimClient:
    """WorkerClient's operations answered in process by the lab's Delivery, as verifier-1."""

    def __init__(self, lab):
        self.lab = lab

    def post(self, operation, body):
        self.lab.calls.append(operation)
        delivery, ids = self.lab.delivery, (body.get("delivery_id"), body.get("lease_token"))
        if operation == "claim":
            return delivery.claim("verifier-1", "verifier")
        if operation == "clock":
            return {}
        if operation == "renew":
            return delivery.renew("verifier-1", "verifier", *ids)
        if operation == "retry":
            return delivery.finish("verifier-1", "verifier", *ids, None, retry=True)
        if operation == "result":
            return delivery.finish("verifier-1", "verifier", *ids, body["result"])
        raise AssertionError(operation)


class InPassSamplingTests(unittest.TestCase):
    def test_be6e636_timeline_a_target_healthy_from_40_s_is_confirmed_in_one_delivery(self):
        # S2 r2 at be6e636: rollback committed, target healthy only from about +40 s. Before the fix the
        # back-off gave readings at about +1, +2, +4, +8, +16, +32, +64 and +128 s: the incident ended
        # FAILED at the 90 s deadline after 8 deliveries of the Verifier command.
        lab = SimLab(self, healthy_from=40)
        stream = io.StringIO()
        worker = lab.worker()
        lab.advance(1)  # The Verifier claims about a second after the write.
        with log.configured("test", level="DEBUG", stream=stream):
            state = lab.run(worker)
        self.assertEqual(state, "RECOVERED")
        self.assertLessEqual(lab.attempts(), 2)
        self.assertLessEqual(lab.attempts(), MAX_ATTEMPTS)
        readings = lab.readings()
        times = [at for at, _ in readings]
        interval = sampling_interval(KIND_HEALTH)
        self.assertEqual(interval, 10)
        gaps = [right - left for left, right in zip(times, times[1:], strict=False)]
        self.assertTrue(all(0 < gap <= interval + 1e-6 for gap in gaps), times)
        first_healthy = next(at for at, status in readings if status == "healthy")
        self.assertGreaterEqual(first_healthy, 40)
        self.assertLess(first_healthy, 40 + interval + 1)
        self.assertTrue(all(status == "unhealthy" for at, status in readings if at < 40))
        # Confirmed within the convergence grace, well before the 90 s deadline.
        self.assertLessEqual(lab.after(), KIND_HEALTH["convergence_grace_seconds"])
        outcome = lab.f.verification.get(lab.action_id)["outcome"]
        self.assertEqual(outcome["status"], "RECOVERED")
        # Each reading is logged with its status alone, never its content.
        lines = [json.loads(line) for line in stream.getvalue().splitlines() if '"verifier.sample"' in line]
        self.assertEqual([line["status"] for line in lines], [status for _, status in readings])
        base = {"ts", "mono", "level", "component", "logger", "event"}
        self.assertTrue(all(set(line) - base <= log.FIELDS for line in lines), lines[0])
        self.assertTrue(all("at" not in line and "template_sha256" not in line for line in lines), lines[0])

    def test_a_target_that_never_becomes_healthy_still_ends_failed_by_the_deadline(self):
        lab = SimLab(self, healthy_from=float("inf"))
        worker = lab.worker()
        lab.advance(1)
        self.assertEqual(lab.run(worker), "FAILED")
        self.assertEqual(lab.f.verification.get(lab.action_id)["outcome"]["status"], "NOT_RECOVERED")
        self.assertLessEqual(lab.attempts(), 2)
        job = lab.f.verification.get(lab.action_id)
        deadline = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00")).timestamp() - lab.t0
        # The first unhealthy reading after the 60 s grace decides it, before the 90 s deadline.
        self.assertLessEqual(lab.after(), deadline)
        self.assertTrue(all(status == "unhealthy" for _, status in lab.readings()))
        self.assertGreater(lab.readings()[-1][0], KIND_HEALTH["convergence_grace_seconds"])

    def test_evidence_that_never_becomes_conclusive_is_frozen_at_the_fixed_deadline(self):
        # Readings that are never complete keep the verdict open; the store freezes UNKNOWN at the deadline.
        lab = SimLab(self, healthy_from=0, incomplete=True)
        worker = lab.worker()
        lab.advance(1)
        self.assertEqual(lab.run(worker), "NEEDS_REVIEW")
        job = lab.f.verification.get(lab.action_id)
        self.assertEqual(job["outcome"]["status"], "UNKNOWN")
        deadline = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00")).timestamp() - lab.t0
        self.assertGreaterEqual(lab.after(), deadline)
        self.assertLess(lab.after(), deadline + 1)  # Decided at the deadline, not a back-off later.
        self.assertEqual(lab.attempts(), 1)
        self.assertTrue(all(at < deadline for at, _ in lab.readings()))

    def test_a_lease_lost_mid_sampling_stops_sampling_and_posts_no_result(self):
        lab = SimLab(self, healthy_from=40)
        worker = lab.worker()
        lab.advance(1)
        reading = lab.reading

        def replaced_after_three(execution):
            if len(lab.readings()) == 2:  # The Coordinator replaces the lease during the third reading.
                lab.f.store.db.execute("UPDATE transport_receipts SET lease_token='replaced'")
            return reading(execution)
        lab.reading = replaced_after_three
        worker.adapter.sample_reader = replaced_after_three
        with patch("runtime.remote_worker.Lease", lab.lease_class()), self.assertRaises(ProtocolError) as error:
            worker.once()
        self.assertEqual(error.exception.status, 409)
        self.assertNotIn("result", lab.calls)
        self.assertEqual(len(lab.readings()), 3)  # No reading after the lost renewal.
        self.assertLess(lab.after(), 1 + 2 * sampling_interval(KIND_HEALTH) + 1.5)  # Seen within a second.
        self.assertIsNone(lab.f.verification.get(lab.action_id)["outcome"])
        self.assertEqual(worker.journal.pending(), [])
        self.assertEqual(lab.state(), "VERIFYING")

    def test_a_lease_check_that_fails_before_a_reading_takes_no_reading(self):
        lab = SimLab(self, healthy_from=40)
        adapter = lab.adapter()
        lease = Mock()
        lease.error = None
        lease.ensure_current.side_effect = [None, None, ProtocolError(409, "Delivery retired or lease expired/replaced")]
        delivery = {"topic": "verifier.request", "payload": {
            "execution": lab.execution, "proposal": lab.f.store.action(lab.action_id)["proposal"]}}
        with self.assertRaises(ProtocolError):
            adapter.process(delivery, lease)
        self.assertEqual(len(lab.readings()), 2)
        self.assertIsNone(lab.f.verification.get(lab.action_id)["outcome"])

    def test_a_verifier_killed_mid_sampling_is_redelivered_and_still_verifies(self):
        lab = SimLab(self, healthy_from=40)
        lab.crash_at = 50  # Dies at its sixth reading, after the first healthy one.
        worker = lab.worker()
        lab.advance(1)
        with self.assertRaises(VerifierCrash):
            lab.run(worker)
        self.assertNotIn("retry", lab.calls)  # A dead process hands nothing back.
        before = lab.readings()
        self.assertEqual(before[-1][1], "healthy")
        started = lab.f.verification.get(lab.action_id)["started_at"]
        # Restart: the same journals reopened, a new adapter and worker; the lease runs out first.
        lab.f.verification.close()
        lab.f.verification = lab.journal()
        lab.leases.clear()
        restarted = lab.worker(lab.adapter(lab.f.verification))
        self.assertEqual(lab.run(restarted), "RECOVERED")
        self.assertEqual(lab.attempts(), 2)
        job = lab.f.verification.get(lab.action_id)
        self.assertEqual(job["started_at"], started)  # The deadline did not move.
        self.assertEqual(lab.readings()[:len(before)], before)
        times = [at for at, _ in lab.readings()]
        self.assertTrue(all(right - left <= KIND_HEALTH["max_sample_gap_seconds"]
                            for left, right in zip(times, times[1:], strict=False)), times)


class PassBoundTests(unittest.TestCase):
    def setUp(self):
        self.lab = SimLab(self, healthy_from=40)
        self.delivery = {"topic": "verifier.request", "payload": {
            "execution": self.lab.execution, "proposal": self.lab.f.store.action(self.lab.action_id)["proposal"]}}
        self.lease = Mock(error=None)

    def test_a_pass_gives_the_delivery_back_before_max_pass_seconds(self):
        adapter = VerifierAdapter(self.lab.f.verification, policy_reader=lambda _: deepcopy(self.lab.policy),
                                  sample_reader=self.lab.reading, clock=self.lab.now, sleep=self.lab.sleep,
                                  max_pass_seconds=25)
        self.assertIsNone(adapter.process(self.delivery, self.lease))
        self.assertEqual([at for at, _ in self.lab.readings()], [0, 10, 20])
        self.assertLessEqual(self.lab.after(), 25)
        # The next pass carries on from the saved readings against the same deadline.
        adapter.max_pass_seconds = 600
        outcome, report = adapter.process(self.delivery, self.lease)
        self.assertEqual((outcome["status"], report), ("RECOVERED", None))

    def test_a_wall_clock_that_does_not_move_ends_the_pass(self):
        frozen = self.lab.now()
        adapter = VerifierAdapter(self.lab.f.verification, policy_reader=lambda _: deepcopy(self.lab.policy),
                                  sample_reader=self.lab.reading, clock=lambda: frozen, sleep=lambda _s: None)
        self.assertIsNone(adapter.process(self.delivery, self.lease))
        self.assertEqual(len(self.lab.readings()), 1)

    def test_a_lease_error_from_the_renewal_thread_interrupts_the_wait(self):
        adapter = VerifierAdapter(self.lab.f.verification, policy_reader=lambda _: deepcopy(self.lab.policy),
                                  sample_reader=self.lab.reading, clock=self.lab.now, sleep=self.lab.sleep)
        lost = ProtocolError(409, "Delivery retired or lease expired/replaced")

        def sleep(seconds):
            self.lab.sleep(seconds)
            if self.lab.after() >= 3:
                self.lease.error = lost
        adapter.sleep = sleep
        with self.assertRaises(ProtocolError):
            adapter.process(self.delivery, self.lease)
        self.assertEqual(self.lab.after(), 3)
        self.assertEqual(len(self.lab.readings()), 1)

    def test_a_shutdown_during_a_wait_hands_the_delivery_back_and_keeps_the_readings(self):
        stopping, stops = threading.Event(), [12]

        def sleep(seconds):
            self.lab.sleep(seconds)
            if stops and self.lab.after() >= stops[0]:
                stops.clear()
                stopping.set()
        adapter = VerifierAdapter(self.lab.f.verification, policy_reader=lambda _: deepcopy(self.lab.policy),
                                  sample_reader=self.lab.reading, clock=self.lab.now, sleep=sleep, stopping=stopping)
        self.assertIsNone(adapter.process(self.delivery, self.lease))
        self.assertEqual([at for at, _ in self.lab.readings()], [0, 10])
        self.assertEqual(self.lab.after(), 12)
        self.assertIsNone(self.lab.f.verification.get(self.lab.action_id)["outcome"])
        stopping.clear()  # A restarted worker carries on from the saved readings.
        outcome, _report = adapter.process(self.delivery, self.lease)
        self.assertEqual(outcome["status"], "RECOVERED")

    def test_max_pass_seconds_is_validated(self):
        for value in (0, -1, float("inf"), 3601, "600", True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                VerifierAdapter(self.lab.f.verification, policy_reader=dict, sample_reader=list, max_pass_seconds=value)


class SamplingIntervalTests(unittest.TestCase):
    def health(self, **rules):
        return {"minimum_samples": 2, "stable_window_seconds": 10, "max_sample_gap_seconds": 20, **rules}

    def test_the_interval_is_at_most_half_the_gap(self):
        self.assertEqual(sampling_interval(self.health()), 10)
        self.assertEqual(sampling_interval(self.health(max_sample_gap_seconds=6)), 3)
        self.assertEqual(sampling_interval(self.health(max_sample_gap_seconds=1)), 0.5)

    def test_the_interval_follows_the_stable_window_over_the_minimum_samples(self):
        self.assertEqual(sampling_interval(self.health(minimum_samples=3, stable_window_seconds=2,
                                                       max_sample_gap_seconds=2)), 1)  # The file-mode policy.
        self.assertEqual(sampling_interval(self.health(minimum_samples=1, stable_window_seconds=4)), 4)

    def test_the_interval_has_a_floor_and_a_ceiling(self):
        self.assertEqual(sampling_interval(self.health(minimum_samples=50, stable_window_seconds=1)),
                         MIN_SAMPLE_INTERVAL_SECONDS)
        self.assertEqual(sampling_interval(self.health(stable_window_seconds=600, max_sample_gap_seconds=600)), 60)


class ReadingStatusTests(unittest.TestCase):
    def setUp(self):
        self.lab = SimLab(self)
        self.policy = deepcopy(self.lab.policy)
        self.policy["health"]["expected_template_sha256"] = self.lab.template

    def status(self, after, **changes):
        self.lab.t = self.lab.t0 + after
        (sample,) = self.lab.reading(self.lab.execution)
        sample.update(changes)
        return reading_status(self.lab.execution, sample, self.policy)

    def test_one_reading_is_healthy_unhealthy_or_incomplete(self):
        self.assertEqual(self.status(45), "healthy")
        self.assertEqual(self.status(5), "unhealthy")
        self.assertEqual(self.status(45, configuration_stable=False), "incomplete")
        self.assertEqual(self.status(45, http_status=None), "incomplete")
        self.assertEqual(self.status(45, template_sha256="b" * 64), "unhealthy")


if __name__ == "__main__":
    unittest.main()
