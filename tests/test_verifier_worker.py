from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from services.verifier.store import VerificationStore
from services.verifier.core import VerifierInputError
from tests.test_verifier_store import fixture

START = datetime(2026, 10, 10, 12, 0, 50, tzinfo=timezone.utc)


def ready_store(path=":memory:"):
    store = SQLiteStore(path)
    bus = LocalEventBus()
    guard = fixture("messages/guard-result.json")
    guard.update(verdict="ALLOW", reason_codes=["VALIDATION_PASSED"])
    for message in (fixture("messages/incident.json"), fixture("messages/proposal.json"),
                    guard, fixture("messages/execution-result.json")):
        handle(message, store, bus)
    return store


class VerifierWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store = ready_store()
        self.ledger = VerificationStore()
        self.addCleanup(self.store.close)
        self.addCleanup(self.ledger.close)
        self.bus = LocalEventBus()
        self.now = START
        self.policy = fixture("policy.json")
        self.readings = fixture("health.json")["stable"]
        self.action = fixture("messages/execution-result.json")["action_id"]

    def run_pass(self, **changes):
        readers = dict(ledger=self.ledger, clock=lambda: self.now,
                       policy_reader=lambda execution: self.policy,
                       sample_reader=lambda execution: [row for row in self.readings
                           if datetime.fromisoformat(row["at"].replace("Z", "+00:00")) <= self.now])
        readers.update(changes)
        return process_verifier_requests(self.store, self.bus, **readers)

    def state(self):
        return self.store.action(self.action)["state"]

    def test_partial_progress_stays_verifying_and_complete_window_recovers(self):
        self.assertEqual(self.run_pass(), [])
        self.assertEqual(self.state(), "VERIFYING")
        self.now += timedelta(seconds=10)
        self.assertEqual(self.run_pass(), [])
        self.now += timedelta(seconds=10)
        result = self.run_pass()
        self.assertEqual(result[0]["status"], "RECOVERED")
        self.assertEqual(self.state(), "RECOVERED")
        self.assertEqual(self.ledger.pending_outcomes(), [])
        self.assertEqual(self.run_pass(), [])

    def test_unhealthy_dependency_routes_to_failed(self):
        self.readings[0]["dependencies"]["mock-db"] = False
        self.assertEqual(self.run_pass()[0]["status"], "NOT_RECOVERED")
        self.assertEqual(self.state(), "FAILED")

    def test_missing_samples_timeout_routes_to_review(self):
        self.readings = []
        self.assertEqual(self.run_pass(), [])
        self.now += timedelta(seconds=65)
        self.assertEqual(self.run_pass()[0]["status"], "UNKNOWN")
        self.assertEqual(self.state(), "NEEDS_REVIEW")

    def test_policy_is_read_once_and_not_changed_during_monitoring(self):
        self.run_pass()
        self.now += timedelta(seconds=20)
        def unavailable(execution):
            raise RuntimeError("Policy source unavailable after intake")
        self.assertEqual(self.run_pass(policy_reader=unavailable)[0]["status"], "RECOVERED")

    def test_other_component_requests_remain_unacknowledged(self):
        incident = fixture("messages/incident.json")
        incident["incident_id"] = "INC-OTHER"
        handle(incident, self.store, self.bus)
        self.run_pass()
        pending = self.store.db.execute("SELECT topic FROM outbox WHERE delivered=0").fetchall()
        self.assertTrue(any(row[0] == "planner.request" for row in pending))

    def test_reader_failure_keeps_request_and_job_pending(self):
        def broken(execution):
            raise RuntimeError("Collector unavailable")
        with self.assertRaises(RuntimeError):
            self.run_pass(sample_reader=broken)
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.ledger.get(self.action)["samples"], [])
        self.assertIsNone(self.ledger.get(self.action)["outcome"])
        self.now += timedelta(seconds=65)
        self.assertEqual(self.run_pass(sample_reader=broken)[0]["status"], "UNKNOWN")

    def test_future_or_wrong_target_readings_are_not_saved(self):
        for row in (dict(self.readings[0], service="other"), self.readings[-1]):
            with self.subTest(row=row), self.assertRaises(VerifierInputError):
                self.run_pass(sample_reader=lambda execution: [row])
        self.assertEqual(self.ledger.get(self.action)["samples"], [])
        self.assertEqual(self.state(), "VERIFYING")

    def test_tampered_request_cannot_start_a_job(self):
        request = self.store.db.execute("SELECT id,payload FROM outbox WHERE topic='verifier.request'").fetchone()
        original = json.loads(request["payload"])
        for field, value in (("incident_id", "other"), ("action_id", "other"),
                             ("target", dict(original["execution"]["target"], uid="other")),
                             ("resource_version", "changed")):
            payload = json.loads(request["payload"])
            payload["execution"][field] = value
            self.store.db.execute("UPDATE outbox SET payload=? WHERE id=?", (json.dumps(payload), request["id"]))
            with self.subTest(field=field), self.assertRaises(VerifierInputError):
                self.run_pass()
        self.assertIsNone(self.ledger.get(self.action))
        self.assertEqual(self.state(), "VERIFYING")

    def test_coordinator_failure_rolls_back_outcome_and_request_ack(self):
        self.now += timedelta(seconds=20)
        from runtime.verifier_durable import record_message as record
        def fail_after_record(message, store):
            record(message, store)
            raise RuntimeError("Crash before Coordinator commit")
        with patch("runtime.verifier_durable.record_message", side_effect=fail_after_record):
            with self.assertRaises(RuntimeError):
                self.run_pass()
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.ledger.pending_outcomes()), 1)
        self.assertEqual(self.store.db.execute(
            "SELECT delivered FROM outbox WHERE topic='verifier.request'"
        ).fetchone()[0], 0)
        saved = self.ledger.pending_outcomes()[0]
        self.assertEqual(self.run_pass(), [saved])
        self.assertEqual(self.state(), "RECOVERED")

    def test_crash_after_coordinator_acceptance_replays_exact_outcome(self):
        self.now += timedelta(seconds=20)
        with patch.object(self.ledger, "acknowledge_outcome", side_effect=RuntimeError("Crash before local ack")):
            with self.assertRaises(RuntimeError):
                self.run_pass()
        self.assertEqual(self.state(), "RECOVERED")
        saved = self.ledger.pending_outcomes()[0]
        self.now += timedelta(seconds=100)
        self.assertEqual(self.run_pass(), [saved])
        self.assertEqual(self.ledger.pending_outcomes(), [])
        count = self.store.db.execute("SELECT count(*) FROM outbox WHERE topic='incident.outcome'").fetchone()[0]
        self.assertEqual(count, 1)

    def test_repeated_routing_envelope_reuses_completed_job(self):
        self.now += timedelta(seconds=20)
        saved = self.run_pass()[0]
        self.store.db.execute("INSERT INTO outbox(topic,payload,action_id) SELECT topic,payload,action_id FROM outbox WHERE topic='verifier.request' LIMIT 1")
        self.assertEqual(self.run_pass(), [saved])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox WHERE topic='incident.outcome'").fetchone()[0], 1)

    def test_publication_failure_does_not_lose_saved_outcome(self):
        self.now += timedelta(seconds=20)
        with patch.object(self.bus, "publish", side_effect=RuntimeError("Bus unavailable")):
            with self.assertRaises(RuntimeError):
                self.run_pass()
        self.assertEqual(self.state(), "RECOVERED")
        self.assertEqual(self.ledger.pending_outcomes(), [])
        self.assertEqual(self.run_pass(), [])
        self.assertTrue(any(event["topic"] == "incident.outcome" for event in self.bus.events))

    def test_checkpoint_and_deadline_survive_full_worker_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator_path = Path(directory) / "coordinator.sqlite3"
            verifier_path = Path(directory) / "verifier.sqlite3"
            coordinator, ledger = ready_store(coordinator_path), VerificationStore(verifier_path)
            try:
                process_verifier_requests(coordinator, self.bus, ledger=ledger,
                    policy_reader=lambda execution: self.policy, sample_reader=lambda execution: self.readings[:2],
                    clock=lambda: START + timedelta(seconds=10))
                saved_deadline = ledger.get(self.action)["deadline"]
            finally:
                coordinator.close()
                ledger.close()

            coordinator, ledger = SQLiteStore(coordinator_path), VerificationStore(verifier_path)
            try:
                results = process_verifier_requests(coordinator, self.bus, ledger=ledger,
                    policy_reader=lambda execution: None, sample_reader=lambda execution: self.readings[2:],
                    clock=lambda: START + timedelta(seconds=20))
                self.assertEqual(results[0]["status"], "RECOVERED")
                self.assertEqual(len(ledger.get(self.action)["samples"]), 3)
                self.assertEqual(ledger.get(self.action)["deadline"], saved_deadline)
            finally:
                coordinator.close()
                ledger.close()

    def test_streaming_reader_checkpoints_before_process_interruption(self):
        class WorkerKilled(BaseException):
            pass

        def interrupted(execution):
            yield self.readings[0]
            raise WorkerKilled()

        with self.assertRaises(WorkerKilled):
            self.run_pass(sample_reader=interrupted)
        saved = self.ledger.get(self.action)
        self.assertEqual(saved["samples"], self.readings[:1])
        self.assertEqual(self.state(), "VERIFYING")
        self.now += timedelta(seconds=20)
        self.assertEqual(self.run_pass(sample_reader=lambda execution: iter(self.readings[1:]))[0]["status"], "RECOVERED")
        self.assertEqual(self.ledger.get(self.action)["deadline"], saved["deadline"])

    def test_overlapping_workers_keep_one_final_outcome(self):
        with tempfile.TemporaryDirectory() as directory:
            cp, vp = Path(directory) / "coordinator.sqlite3", Path(directory) / "verifier.sqlite3"
            first, second = ready_store(cp), SQLiteStore(cp)
            ledger1, ledger2 = VerificationStore(vp), VerificationStore(vp)
            try:
                def second_finishes_first(execution):
                    process_verifier_requests(second, self.bus, ledger=ledger2,
                        policy_reader=lambda execution: self.policy, sample_reader=lambda execution: self.readings,
                        clock=lambda: START + timedelta(seconds=20))
                    return fixture("health.json")["unstable"]
                result = process_verifier_requests(first, self.bus, ledger=ledger1,
                    policy_reader=lambda execution: self.policy, sample_reader=second_finishes_first,
                    clock=lambda: START + timedelta(seconds=20))
                self.assertEqual(result[0]["status"], "RECOVERED")
                self.assertEqual(first.db.execute("SELECT count(*) FROM outbox WHERE topic='incident.outcome'").fetchone()[0], 1)
            finally:
                for connection in (first, second, ledger1, ledger2):
                    connection.close()

    def test_coordinator_and_verifier_must_not_share_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "shared.sqlite3"
            coordinator, ledger = ready_store(path), VerificationStore(path)
            try:
                with self.assertRaises(VerifierInputError):
                    process_verifier_requests(coordinator, self.bus, ledger=ledger,
                        policy_reader=lambda execution: self.policy, sample_reader=lambda execution: [], clock=lambda: START)
            finally:
                coordinator.close()
                ledger.close()
