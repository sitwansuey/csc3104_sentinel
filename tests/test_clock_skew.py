"""Lifecycle ordering by Coordinator sequence and the measured clock-skew bound (P3.03).

Worker results are ordered by the outbox request they answer, not by the
producers' clocks. The remaining time checks use an explicit skew bound that
workers measure against the Coordinator (Cristian's algorithm) and fail closed
on. Every role here runs in one process on one host: skew is simulated by
giving each role its own clock, not measured between machines.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, UTC
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.coordinator_server import servers
from runtime.remote_adapters import ExecutorAdapter, GuardAdapter, PlannerAdapter, VerifierAdapter
from runtime.remote_worker import Lease, RemoteIngress, RemoteWorker, WorkerJournal, lease_duration
from runtime.worker_health import WorkerHealth
from runtime.worker_http import ClockEstimator, WorkerClient, WorkerHTTPServer, handler, serve_until_stopped
from runtime.worker_transport import Delivery, ProtocolError, digest, parse_clock_estimate
from services.coordinator.core import LocalEventBus, approve, handle, record_message
from services.coordinator.store import (
    CoordinatorError, MIGRATIONS, SCHEMA_VERSION, SQLiteStore, SupersededResult, schema_version,
)
from services.guard.core import GuardInputError, evaluate, proposal_fingerprint
from services.guard.decisions import validate_policy
from services.observer.core import detect
from services.planner.core import mock_provider
from services.planner.store import PlannerStore
from services.verifier.core import inspect_dependencies

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_V0 = Path(__file__).parent / "fixtures/coordinator_schema_v0.sql"
STAMPS = ("checked_at", "valid_until", "recorded_at", "window_start", "window_end")


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples/messages" / (name + ".json")).read_text(encoding="utf-8"))


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def shifted(message, seconds):
    """A copy whose own timestamps are moved by seconds, as a skewed producer would write them."""
    result = deepcopy(message)
    for key in STAMPS:
        if key in result:
            moved = datetime.fromisoformat(result[key].replace("Z", "+00:00")) + timedelta(seconds=seconds)
            result[key] = stamp(moved)
    return result


def allow():
    """A version 2.0 ALLOW bound to the sample proposal, as the Guard writes it."""
    result = sample("guard-result")
    result.update(schema_version="2.0", verdict="ALLOW", reason_codes=["VALIDATION_PASSED"],
                  proposal_sha256=digest(sample("proposal")), inputs_sha256="1" * 64,
                  valid_until="2026-10-10T12:01:05Z")
    return result


class SequenceOrderingTests(unittest.TestCase):
    """The worker transport in one process: results carry the request they answer."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "coordinator.db"
        self.coordinator_now = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
        self.store = SQLiteStore(self.path, clock=lambda: self.coordinator_now)
        self.addCleanup(self.store.close)
        self.lease_now = 1000.0
        self.delivery = Delivery(self.store, clock=lambda: self.lease_now, lease_seconds=30,
                                 dependency_requests=True)
        self.incident, self.proposal = sample("incident"), sample("proposal")

    def claim(self, role, identity=None):
        delivery = self.delivery.claim(identity or role, role)["delivery"]
        self.assertIsNotNone(delivery, role + " found no work")
        return delivery

    def finish(self, role, delivery, result, identity=None):
        report = None
        if role == "guard":
            latest = self.store.dependency_result(result["action_id"])
            report = {"decision_sha256": digest(result), "proposal_sha256": digest(self.proposal),
                      "inputs_sha256": result.get("inputs_sha256"),
                      "input_fingerprints": {"dependencies": digest(latest) if latest else None}}
        return self.delivery.finish(identity or role, role, delivery["delivery_id"], delivery["lease_token"],
                                    result, report=report)

    def state(self):
        return self.store.incident(self.incident["incident_id"])["state"]

    def to_validating(self, verifier_offset=-600):
        self.delivery.ingest("observer", self.incident, identity="observer")
        self.finish("planner", self.claim("planner"), self.proposal)
        request = self.claim("verifier")
        self.assertEqual(request["topic"], "dependency.request")
        self.finish("verifier", request, shifted(sample("dependency-result"), verifier_offset))

    def to_ready(self, guard_offset=300):
        self.to_validating()
        request = self.claim("guard")
        self.finish("guard", request, shifted(allow(), guard_offset))
        self.assertEqual(self.state(), "READY")
        return request

    def test_skewed_producers_reach_recovered_in_sequence_mode(self):
        # Guard 300 s ahead, Executor 300 s behind, Verifier 600 s behind the Coordinator.
        self.to_ready(guard_offset=300)
        executor = self.claim("executor")
        self.finish("executor", executor, shifted(sample("execution-result"), -300))
        self.assertEqual(self.state(), "VERIFYING")
        verifier = self.claim("verifier")
        self.assertEqual(verifier["topic"], "verifier.request")
        self.finish("verifier", verifier, shifted(sample("outcome"), -600))
        self.assertEqual(self.state(), "RECOVERED")
        meta = self.store.accepted_meta("outcome", self.proposal["action_id"])
        self.assertEqual(meta["answers"], verifier["event_id"])
        self.assertEqual(meta["producer"], "verifier")
        seqs = [row[0] for row in self.store.db.execute("SELECT seq FROM messages ORDER BY rowid")]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(self.store.incident(self.incident["incident_id"])["high_water"], seqs[-1])

    def test_single_clock_mode_still_refuses_the_same_skewed_results(self):
        # handle() has no request to order by, so it keeps comparing timestamps (unchanged behaviour).
        bus = LocalEventBus()
        handle(self.incident, self.store, bus)
        handle(self.proposal, self.store, bus)
        handle(shifted(allow(), 300), self.store, bus)
        with self.assertRaisesRegex(CoordinatorError, "Execution predates Guard validation"):
            handle(shifted(sample("execution-result"), -300), self.store, bus)
        self.assertEqual(self.state(), "READY")

    def test_a_guard_result_answering_a_superseded_request_is_rejected_even_when_later(self):
        self.to_validating()
        stale = self.claim("guard", "guard-a")
        # New evidence arrives: the Coordinator issues a newer guard.request.
        self.delivery.ingest("verifier", shifted(sample("dependency-result"), 5), identity="verifier")
        late = shifted(allow(), 3600)  # Stamped an hour later than anything else.
        with self.store.transaction(), self.assertRaisesRegex(SupersededResult, "superseded request"):
            record_message(late, self.store, answers=stale["event_id"], producer="guard-a")
        with self.assertRaises(ProtocolError) as refused:
            self.finish("guard", stale, late, identity="guard-a")
        self.assertEqual(refused.exception.status, 409)  # Released by the worker, never resent for ever.
        self.assertEqual(self.state(), "VALIDATING")
        current = self.claim("guard", "guard-b")
        self.assertGreater(current["event_id"], stale["event_id"])
        self.finish("guard", current, shifted(allow(), -3600), identity="guard-b")  # Earlier stamp, current request.
        self.assertEqual(self.state(), "READY")

    def test_an_execution_answering_a_request_issued_before_the_current_guard_decision_is_rejected(self):
        self.to_ready()
        old = self.claim("executor", "executor-a")
        self.delivery.ingest("verifier", shifted(sample("dependency-result"), 5), identity="verifier")
        self.assertEqual(self.state(), "VALIDATING")
        self.finish("guard", self.claim("guard"), shifted(allow(), 600))
        self.assertEqual(self.state(), "READY")
        late = shifted(sample("execution-result"), 3600)
        with self.store.transaction(), self.assertRaises(SupersededResult):
            record_message(late, self.store, answers=old["event_id"], producer="executor-a")
        with self.assertRaises(ProtocolError) as refused:
            self.finish("executor", old, late, identity="executor-a")
        self.assertEqual(refused.exception.status, 409)
        current = self.claim("executor", "executor-b")
        self.finish("executor", current, shifted(sample("execution-result"), -3600), identity="executor-b")
        self.assertEqual(self.state(), "VERIFYING")

    def test_a_superseded_result_is_answered_with_409_so_the_worker_releases_it(self):
        # dispatch_pending usually retires a superseded request first; this is the
        # transport's answer when the ordering rule itself refuses the result.
        self.to_validating()
        request = self.claim("guard")
        with patch("runtime.worker_transport.record_message", side_effect=SupersededResult("stale")), \
                self.assertRaises(ProtocolError) as refused:
            self.finish("guard", request, allow())
        self.assertEqual((refused.exception.status, str(refused.exception)),
                         (409, "Result answers a superseded request"))
        self.assertEqual(self.state(), "VALIDATING")

    def test_an_outcome_must_answer_the_current_verifier_request(self):
        self.to_ready()
        executor = self.claim("executor")
        self.finish("executor", executor, sample("execution-result"))
        with self.store.transaction(), self.assertRaisesRegex(SupersededResult, "superseded request"):
            record_message(sample("outcome"), self.store, answers=executor["event_id"], producer="verifier")
        self.assertEqual(self.state(), "VERIFYING")

    def test_a_result_answering_a_request_issued_before_the_stored_result_is_rejected(self):
        self.to_ready()
        guard_request = self.store.accepted_meta("guard_result", self.proposal["action_id"])["answers"]
        with self.store.transaction(), self.assertRaises(SupersededResult):
            # The same request again (a forged or duplicated answer with a new payload).
            record_message(shifted(allow(), 900), self.store, answers=guard_request, producer="guard")

    def test_approval_is_not_compared_with_a_sequenced_guard_clock(self):
        self.to_validating()
        escalate = shifted(sample("guard-result"), 300)  # The Guard's clock is 300 s ahead.
        self.finish("guard", self.claim("guard"), escalate)
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        record = approve(self.proposal["action_id"], "operator-1", self.store)
        self.assertEqual(record["approved_at"], stamp(self.coordinator_now))
        self.assertEqual(self.state(), "VALIDATING")

    def test_single_clock_approval_still_refuses_a_guard_decision_from_its_future(self):
        bus = LocalEventBus()
        handle(self.incident, self.store, bus)
        handle(self.proposal, self.store, bus)
        handle(shifted(sample("guard-result"), 300), self.store, bus)
        with self.assertRaisesRegex(CoordinatorError, "Approval predates the Guard decision"):
            approve(self.proposal["action_id"], "operator-1", self.store)

    def test_ingress_findings_compare_timestamps_only_from_one_producer(self):
        self.to_validating()
        finding = sample("dependency-result")
        self.delivery.ingest("verifier", shifted(finding, 10), identity="verifier-a")
        # Another producer's clock is behind: acceptance order decides, not its stamp.
        self.delivery.ingest("verifier", shifted(finding, -120), identity="verifier-b")
        self.assertEqual(self.store.dependency_result(self.proposal["action_id"]), shifted(finding, -120))
        with self.assertRaisesRegex(CoordinatorError, "newer than stored evidence"):
            self.delivery.ingest("verifier", shifted(finding, -180), identity="verifier-b")  # One clock went back.


class ClockEstimatorTests(unittest.TestCase):
    def test_cristians_estimate_uses_the_monotonic_round_trip_and_keeps_the_tightest_sample(self):
        wall, mono = [100.0], [10.0]
        estimator = ClockEstimator(clock=lambda: wall[0], monotonic=lambda: mono[0])
        started = estimator.start()
        mono[0] += 0.2
        offset, uncertainty = estimator.observe(started, "160.1")
        self.assertAlmostEqual(offset, 60.0)
        self.assertAlmostEqual(uncertainty, 0.1)
        started = estimator.start()
        mono[0] += 2.0
        estimator.observe(started, "170.0")  # A slower exchange is kept but not preferred.
        self.assertAlmostEqual(estimator.estimate()[0], 60.0)
        self.assertAlmostEqual(estimator.estimate()[1], 0.1)
        self.assertIsNone(estimator.observe(estimator.start(), None))
        self.assertIsNone(estimator.observe(estimator.start(), "nan"))
        for index in range(8):  # Only the last 8 samples count.
            started = estimator.start()
            mono[0] += 1.0
            estimator.observe(started, str(105.0 + index))
        self.assertAlmostEqual(estimator.estimate()[1], 0.5)

    def test_malformed_estimates_are_ignored(self):
        self.assertEqual(parse_clock_estimate("1.5;0.25"), (1.5, 0.25))
        for value in (None, "", "1", "1;2;3", "a;b", "nan;0", "1;-1", "1e9;0", "0;inf"):
            with self.subTest(value=value):
                self.assertIsNone(parse_clock_estimate(value))


class SkewBoundTests(unittest.TestCase):
    """The bound epsilon = 2 s, enforced by the worker and by the Coordinator."""

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        directory = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.database = directory / "coordinator.db"
        self.directory = directory
        self.services = {"planner": ("planner", "p" * 40), "observer": ("observer", "o" * 40)}
        self.server_clock = datetime.now(UTC)
        stopping = threading.Event()
        with SQLiteStore(self.database) as store:
            Delivery(store)
        self.server = stack.enter_context(WorkerHTTPServer(("127.0.0.1", 0), handler(
            self.database, self.services, stopping=stopping, max_clock_skew=None)))
        thread = threading.Thread(target=serve_until_stopped, args=(self.server, stopping))
        thread.start()
        stack.callback(thread.join)
        stack.callback(stopping.set)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        RemoteIngress(self.client("observer"))(sample("incident"))

    def client(self, role, offset=0.0):
        return WorkerClient(self.url, role, self.services[role][1], timeout=5,
                            clock=lambda: time.time() + offset)

    def worker(self, client):
        adapter = Mock()
        adapter.process.side_effect = lambda delivery, lease: (sample("proposal"), None)
        journal = WorkerJournal(self.directory / f"journal-{id(client)}.db")
        self.addCleanup(journal.close)
        return RemoteWorker(client, journal, adapter, max_clock_skew=2), adapter

    def receipts(self):
        with SQLiteStore(self.database) as store:
            return store.db.execute("SELECT count(*) FROM transport_receipts").fetchone()[0]

    def clocks(self):
        with SQLiteStore(self.database) as store:
            return store.worker_clocks()

    def test_a_worker_5_s_behind_refuses_to_claim_and_one_1_s_behind_proceeds(self):
        worker, adapter = self.worker(self.client("planner", offset=-5))
        status = worker.once()
        self.assertEqual(status["status"], "clock_skew")
        self.assertAlmostEqual(status["offset"], 5, delta=0.5)
        self.assertEqual((self.receipts(), adapter.process.call_count), (0, 0))  # Nothing was claimed.
        self.assertAlmostEqual(self.clocks()["planner"]["offset"], 5, delta=0.5)  # Reported and stored.
        worker, adapter = self.worker(self.client("planner", offset=-1))
        self.assertEqual(worker.once()["status"], "accepted")
        self.assertEqual(adapter.process.call_count, 1)
        recorded = self.clocks()["planner"]
        self.assertAlmostEqual(recorded["offset"], 1, delta=0.5)
        self.assertAlmostEqual(recorded["max_abs_offset"], 5, delta=0.5)

    def test_a_worker_ahead_of_the_coordinator_also_fails_closed(self):
        worker, adapter = self.worker(self.client("planner", offset=5))
        self.assertEqual(worker.once()["status"], "clock_skew")
        self.assertEqual(adapter.process.call_count, 0)

    def test_a_large_round_trip_widens_the_uncertainty_and_fails_closed(self):
        client = self.client("planner")
        ticks = iter(range(0, 10_000, 6))  # Every exchange appears to take 6 s: uncertainty 3 s.
        client.clock.monotonic = lambda: float(next(ticks))
        worker, adapter = self.worker(client)
        status = worker.once()
        self.assertEqual(status["status"], "clock_skew")
        self.assertLess(abs(status["offset"]), 4)
        self.assertAlmostEqual(status["uncertainty"], 3)
        self.assertEqual(adapter.process.call_count, 0)

    def test_a_corrected_clock_recovers_after_the_skewed_samples_age_out(self):
        offset = [-5.0]
        client = WorkerClient(self.url, "planner", self.services["planner"][1], timeout=5,
                              clock=lambda: time.time() + offset[0])
        worker, adapter = self.worker(client)
        self.assertEqual(worker.once()["status"], "clock_skew")
        offset[0] = 0.0
        statuses = [worker.once()["status"] for _ in range(9)]
        self.assertIn("accepted", statuses)  # Each refused pass measures again.

    def test_readiness_is_false_while_the_clock_is_out_of_bound(self):
        health = WorkerHealth(ready_seconds=30, clock=lambda: 100.0)
        health.configured()
        health.contacted()
        health.clock_checked(False)
        self.assertEqual(health.ready(), (503, {"ready": False, "contact_age_seconds": 0.0, "clock_skew": True}))
        health.clock_checked(True)
        self.assertEqual(health.ready(), (200, {"ready": True, "contact_age_seconds": 0.0}))

    def test_the_coordinator_refuses_claims_outside_the_bound_and_records_the_estimate(self):
        with SQLiteStore(self.database) as store:
            delivery = Delivery(store, max_clock_skew=2)
            for estimate in (None, (5.0, 0.01), (-1.5, 0.6)):
                with self.subTest(estimate=estimate), self.assertRaises(ProtocolError) as refused:
                    delivery.claim("planner-x", "planner", clock_estimate=estimate)
                self.assertEqual((refused.exception.status, str(refused.exception)),
                                 (409, "Worker clock skew exceeds the bound"))
            self.assertIsNotNone(delivery.claim("planner-x", "planner", clock_estimate=(1.0, 0.01))["delivery"])
            recorded = store.worker_clocks()["planner-x"]
        self.assertEqual((recorded["offset"], recorded["reports"], recorded["max_abs_offset"]), (1.0, 3, 5.0))
        with self.assertRaises(ValueError):
            Delivery(SQLiteStore(":memory:"), max_clock_skew=0)

    def test_a_worker_without_its_own_bound_is_refused_over_http_by_the_coordinators_bound(self):
        stopping = threading.Event()
        with WorkerHTTPServer(("127.0.0.1", 0), handler(self.database, self.services, stopping=stopping,
                                                        max_clock_skew=2)) as server:
            thread = threading.Thread(target=serve_until_stopped, args=(server, stopping))
            thread.start()
            try:
                client = WorkerClient(f"http://127.0.0.1:{server.server_port}", "planner",
                                      self.services["planner"][1], timeout=5, clock=lambda: time.time() - 5)
                with self.assertRaises(ProtocolError) as first:
                    client.post("claim", {})  # No estimate yet: refused, but the reply is measured.
                with self.assertRaises(ProtocolError) as second:
                    client.post("claim", {})  # A 5 s estimate: refused.
                self.assertEqual((first.exception.status, second.exception.status), (409, 409))
                ahead = WorkerClient(f"http://127.0.0.1:{server.server_port}", "planner",
                                     self.services["planner"][1], timeout=5)
                with self.assertRaises(ProtocolError):
                    ahead.post("claim", {})
                self.assertIsNotNone(ahead.post("claim", {})["delivery"])
            finally:
                stopping.set()
                thread.join()


class GuardSkewToleranceTests(unittest.TestCase):
    NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)

    def setUp(self):
        self.proposal = sample("proposal")
        self.live_state = fixture("observations.json")["failed-rollout"][-1]
        self.policy = dict(fixture("policy.json"), max_clock_skew_seconds=2)
        self.baseline = fixture("baseline.json")
        self.dependencies = sample("dependency-result")

    def approval(self, approved_at, expires_at):
        return {"action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
                "policy_version": self.policy["policy_version"], "operator_id": "operator-1",
                "decision": "APPROVED", "approved_at": stamp(approved_at), "expires_at": stamp(expires_at)}

    def run_guard(self, *, dependency_ahead=0, approval=None, policy=None):
        dependencies = dict(self.dependencies, checked_at=stamp(self.NOW + timedelta(seconds=dependency_ahead)))
        return evaluate(self.proposal, self.live_state, policy or self.policy, self.baseline, dependencies,
                        approval, now=self.NOW)

    def test_a_finding_from_up_to_epsilon_in_the_future_is_accepted_and_beyond_it_is_unknown(self):
        self.assertNotIn("DEPENDENCY_RESULT_FROM_FUTURE", self.run_guard(dependency_ahead=1)["reason_codes"])
        self.assertIn("DEPENDENCY_RESULT_FROM_FUTURE", self.run_guard(dependency_ahead=5)["reason_codes"])
        without = dict(self.policy)
        del without["max_clock_skew_seconds"]  # Default 0: today's strict comparison.
        self.assertIn("DEPENDENCY_RESULT_FROM_FUTURE",
                      self.run_guard(dependency_ahead=1, policy=without)["reason_codes"])

    def test_skew_makes_evidence_older_never_younger(self):
        policy = dict(self.policy, max_dependency_age_seconds=10)
        self.assertNotIn("STALE_DEPENDENCY_RESULT", self.run_guard(dependency_ahead=-7, policy=policy)["reason_codes"])
        self.assertIn("STALE_DEPENDENCY_RESULT", self.run_guard(dependency_ahead=-9, policy=policy)["reason_codes"])
        decision = self.run_guard(dependency_ahead=-7, policy=policy)
        # valid_until is brought forward by the bound too: finding + 10 s - 2 s.
        self.assertEqual(decision["valid_until"], stamp(self.NOW - timedelta(seconds=7) + timedelta(seconds=8)))

    def test_an_approval_up_to_epsilon_ahead_is_accepted_and_expiry_comes_forward(self):
        later = self.NOW + timedelta(seconds=120)
        ahead = self.run_guard(approval=self.approval(self.NOW + timedelta(seconds=1), later))
        self.assertEqual(ahead["verdict"], "ALLOW")
        self.assertIn("APPROVAL_FROM_FUTURE", self.run_guard(
            approval=self.approval(self.NOW + timedelta(seconds=5), later))["reason_codes"])
        # One second left on the approval is inside the bound: treated as expired (fail-safe).
        self.assertIn("APPROVAL_EXPIRED", self.run_guard(
            approval=self.approval(self.NOW - timedelta(seconds=60), self.NOW + timedelta(seconds=1)))["reason_codes"])
        self.assertEqual(ahead["valid_until"], stamp(min(self.NOW + timedelta(seconds=30), later - timedelta(seconds=2))))

    def test_the_bound_is_validated(self):
        for value in (-1, 61, 1.5, "2", True):
            with self.subTest(value=value), self.assertRaises(GuardInputError):
                validate_policy(dict(self.policy, max_clock_skew_seconds=value))
        validate_policy(dict(self.policy, max_clock_skew_seconds=60))


class LeaseRenewalTests(unittest.TestCase):
    def lease(self, delivery, worker_offset):
        return Lease(Mock(), delivery, interval=5, clock=lambda: time.time() + worker_offset)

    def test_renewal_is_correct_when_the_worker_clock_is_an_hour_off(self):
        now = time.time()
        delivery = {"delivery_id": "d", "lease_token": "t", "expires_at": now + 3, "expires_in": 3}
        for offset in (-3600, 3600, 0):
            with self.subTest(offset=offset):
                self.assertAlmostEqual(self.lease(delivery, offset).interval, 1.0)

    def test_without_expires_in_the_old_cross_clock_rule_is_the_fallback(self):
        now = time.time()
        legacy = {"delivery_id": "d", "lease_token": "t", "expires_at": now + 3}
        self.assertEqual(self.lease(legacy, -3600).interval, 5)  # Renews after the 3 s lease ended.
        self.assertEqual(self.lease(legacy, 3600).interval, 0.05)  # Renews twenty times a second.
        self.assertAlmostEqual(lease_duration(legacy, clock=lambda: now), 3)

    def test_claim_and_renew_answer_with_a_duration_on_the_coordinators_clock(self):
        with tempfile.TemporaryDirectory() as directory, SQLiteStore(Path(directory) / "c.db") as store:
            delivery = Delivery(store, clock=lambda: 50.0, lease_seconds=7)
            delivery.ingest("observer", sample("incident"))
            claimed = delivery.claim("planner", "planner")["delivery"]
            self.assertEqual((claimed["expires_at"], claimed["expires_in"]), (57.0, 7))
            renewed = delivery.renew("planner", "planner", claimed["delivery_id"], claimed["lease_token"])
            self.assertEqual(renewed, {"expires_at": 57.0, "expires_in": 7})


class SequenceMigrationTests(unittest.TestCase):
    def test_an_old_database_gains_sequence_columns_and_replays_its_outbox_in_sequence_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            db = sqlite3.connect(path)
            db.executescript(FIXTURE_V0.read_text(encoding="utf-8"))
            old_messages = db.execute("SELECT rowid FROM messages ORDER BY rowid").fetchall()
            db.close()
            proposal = sample("proposal")
            with SQLiteStore(path) as store:
                self.assertEqual(schema_version(store.db), SCHEMA_VERSION)
                self.assertGreaterEqual(SCHEMA_VERSION, 4)
                rows = store.db.execute("SELECT rowid,seq,answers,high_water FROM messages ORDER BY rowid").fetchall()
                self.assertEqual([(row[0], row[1], row[2], row[3]) for row in rows],
                                 [(row[0], row[0], None, None) for row in old_messages])
                delivery = Delivery(store, dependency_requests=True)
                request = delivery.claim("verifier", "verifier")["delivery"]
                self.assertEqual(request["topic"], "dependency.request")
                delivery.finish("verifier", "verifier", request["delivery_id"], request["lease_token"],
                                shifted(sample("dependency-result"), -600))
                meta = store.accepted_meta("dependency_result", proposal["action_id"])
                self.assertEqual((meta["seq"], meta["answers"]), (len(old_messages) + 1, request["event_id"]))
                self.assertEqual(store.incident(proposal["incident_id"])["high_water"], meta["seq"])
                self.assertEqual(store.worker_clocks(), {})

    def test_migration_4_applies_once_on_a_version_3_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            db = sqlite3.connect(path, isolation_level=None)
            from services.coordinator.store import migrate
            migrate(db, MIGRATIONS[:3])
            db.execute("INSERT INTO messages(event_key,payload) VALUES('k1','{}'),('k2','{}')")
            db.close()
            for _ in range(2):
                with SQLiteStore(path) as store:
                    self.assertEqual([tuple(row) for row in store.db.execute(
                        "SELECT event_key,seq FROM messages ORDER BY seq")], [("k1", 1), ("k2", 2)])


class LoopbackSkewFlowTests(unittest.TestCase):
    """All six components over loopback HTTP with per-role clocks.

    Coordinator, Planner and Observer on the reference clock; Guard 300 s
    ahead; Executor and Verifier 300 s behind. Before P3.03 the Coordinator
    refused the Executor's applied result (stamped before the Guard ALLOW)
    with HTTP 400 and the worker resent it for ever, so the incident stayed
    READY. Each worker's measured offset is recorded by the Coordinator.
    """
    OFFSETS = {"planner": 0, "observer": 0, "guard": 300, "executor": -300, "verifier": -300}

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.f = OfflineFlow(directory)
        self.stack.callback(self.f.close)
        # No operator approval in this flow: an approval is stamped by the
        # Coordinator, and a check 300 s behind it would see it as from the
        # future, which the skew bound exists to refuse.
        self.f.policy = dict(self.f.policy, require_operator_approval=False)
        self.f.baseline = dict(self.f.baseline, healthy_observed_at="2026-10-10T11:50:00Z")
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        self.services = {role: (role, role[0] * 40) for role in self.OFFSETS}
        approval_services = {role: (role, "a" + role[0] * 40) for role in ("operator", "guard", "executor")}
        permissions = {role: [{"target": self.incident["target"], "operations": ["read"]}] for role in approval_services}
        self.workers, _approvals, _stopping, _ = self.stack.enter_context(servers(
            self.f.path / "coordinator.db", self.services, approval_services, permissions,
            port=0, approval_port=0, clock=lambda: self.f.now, lease_seconds=5))
        self.url = f"http://127.0.0.1:{self.workers.server_port}"
        self.f.cluster.clock = self.clock("executor")  # The Executor reads the Deployment with its own clock.
        self.f.adapters = replace(self.f.adapters, clock=self.clock("executor"))
        executor_client = self.client("executor")
        self.f.authorizer.dependency_reader = lambda p: executor_client.post("dependencies", {"proposal": p})["dependencies"]
        self.f.authorizer.approval_reader = lambda p: None
        self.f.authorizer.policy_reader = lambda p: self.f.policy
        self.f.authorizer.baseline_reader = lambda p: self.f.baseline
        guard_client = self.client("guard")

        def guard_state(proposal):
            state = self.f.cluster.read(proposal["target"])
            state["captured_at"] = stamp(self.clock("guard")())
            return state
        self.guard = GuardAdapter(state_reader=guard_state, policy_reader=lambda _: self.f.policy,
            baseline_reader=lambda _: self.f.baseline,
            dependency_reader=lambda p: guard_client.post("dependencies", {"proposal": p})["dependencies"],
            approval_reader=lambda _: None, clock=self.clock("guard"))
        self.planning = PlannerStore(self.f.path / "planner.db", clock=lambda: self.f.now)
        self.stack.callback(self.planning.close)
        self.planner = PlannerAdapter(self.planning, baseline_reader=lambda _: [self.f.baseline], provider=mock_provider)
        verifier_clock = self.clock("verifier")

        def samples(_execution):
            # The collector stamps readings with the Verifier's own clock.
            return [shifted_reading(reading, self.OFFSETS["verifier"]) for reading in fixture("health.json")["stable"]]
        self.verifier = VerifierAdapter(self.f.verification, policy_reader=lambda _: self.f.policy,
            sample_reader=samples, clock=verifier_clock,
            dependency_reader=lambda p: inspect_dependencies(p, self.f.graph, self.f.cluster.read(p["target"]),
                                                             now=verifier_clock()))

    def clock(self, role):
        return lambda: self.f.now + timedelta(seconds=self.OFFSETS[role])

    def client(self, role):
        role_clock = self.clock(role)
        return WorkerClient(self.url, role, self.services[role][1], timeout=5,
                            clock=lambda: role_clock().timestamp())

    def worker(self, role, adapter):
        journal = WorkerJournal(self.f.path / (role + "-transport.db"))
        self.stack.callback(journal.close)
        return RemoteWorker(self.client(role), journal, adapter, renewal_interval=0.1)

    def state(self):
        return self.f.store.incident(self.incident["incident_id"])["state"]

    def test_per_role_clocks_300_s_apart_reach_recovered(self):
        RemoteIngress(self.client("observer"))(self.incident)
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        verifier = self.worker("verifier", self.verifier)
        self.assertEqual(verifier.once()["status"], "accepted")
        self.assertEqual(verifier.current["topic"], "dependency.request")  # Evidence for the Guard.
        guard = self.worker("guard", self.guard)
        self.assertEqual(guard.once()["status"], "accepted")
        self.assertEqual(self.state(), "READY")
        self.f.now += timedelta(seconds=2)
        executor = self.worker("executor", ExecutorAdapter(self.f.adapters))
        self.assertEqual(executor.once()["status"], "accepted")  # Before P3.03: HTTP 400, for ever.
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.f.cluster.writes), 1)
        action = self.f.store.action(self.f.store.incident(self.incident["incident_id"])["active_action_id"])
        self.assertLess(action["execution_result"]["recorded_at"], action["guard_result"]["checked_at"])
        self.f.now = self.f.now.replace(minute=1, second=10)
        self.assertEqual(verifier.once()["status"], "accepted")
        self.assertEqual(self.state(), "RECOVERED")
        self.assertEqual(len(self.f.cluster.writes), 1)
        measured = self.f.store.worker_clocks()
        for role in ("guard", "executor", "verifier"):
            with self.subTest(role=role):
                # offset = Coordinator time - worker time, within the measured uncertainty.
                self.assertAlmostEqual(measured[role]["offset"], -self.OFFSETS[role],
                                       delta=measured[role]["uncertainty"] + 0.01)


def shifted_reading(reading, seconds):
    moved = datetime.fromisoformat(reading["at"].replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return dict(reading, at=stamp(moved))


if __name__ == "__main__":
    unittest.main()
