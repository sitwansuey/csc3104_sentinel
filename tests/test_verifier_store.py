from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from services.verifier.core import VerifierInputError
from services.verifier.store import VerificationStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 45, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class VerificationStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = VerificationStore()
        self.addCleanup(self.store.close)
        self.execution = fixture("messages/execution-result.json")
        self.policy = fixture("policy.json")

    def start(self):
        return self.store.start(self.execution, self.policy, now=NOW)

    def test_new_job_freezes_inputs_and_initial_deadline(self):
        job = self.start()
        self.assertEqual(job["execution"], self.execution)
        self.assertEqual(job["samples"], [])
        self.assertIsNone(job["outcome"])
        self.assertEqual(job["deadline"], "2026-10-10T12:01:50Z")

    def test_duplicate_request_does_not_extend_deadline(self):
        first = self.start()
        repeated = self.store.start(self.execution, self.policy, now=NOW + timedelta(seconds=10))
        self.assertEqual(repeated, first)

    def test_changed_execution_or_policy_is_rejected(self):
        self.start()
        for execution, policy in (
            (dict(self.execution, resource_version="changed"), self.policy),
            (self.execution, dict(self.policy, policy_version="changed")),
        ):
            with self.subTest(execution=execution), self.assertRaises(VerifierInputError):
                self.store.start(execution, policy, now=NOW)

    def test_job_survives_reopening_the_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verifier.sqlite3"
            first = VerificationStore(path)
            try:
                saved = first.start(self.execution, self.policy, now=NOW)
            finally:
                first.close()
            reopened = VerificationStore(path)
            try:
                self.assertEqual(reopened.get(self.execution["action_id"]), saved)
            finally:
                reopened.close()

    def test_unapplied_execution_and_invalid_clock_are_rejected(self):
        for status in ("FAILED", "CONFLICT", "SUPERSEDED", "NEEDS_RECONCILIATION"):
            with self.subTest(status=status), self.assertRaises(VerifierInputError):
                self.store.start(dict(self.execution, status=status), self.policy, now=NOW)
        with self.assertRaises(VerifierInputError):
            self.store.start(self.execution, self.policy, now=NOW - timedelta(seconds=1))

    def test_invalid_timeout_is_rejected(self):
        for timeout in (True, -1, 19):
            policy = deepcopy(self.policy)
            policy["health"]["verification_timeout_seconds"] = timeout
            with self.subTest(timeout=timeout), self.assertRaises(VerifierInputError):
                self.store.start(self.execution, policy, now=NOW)

    def test_duplicate_batches_do_not_increase_sample_count(self):
        self.start()
        readings = fixture("health.json")["stable"]
        for _ in range(2):
            job = self.store.record_samples(self.execution["action_id"], readings, now=NOW + timedelta(seconds=30))
        self.assertEqual(len(job["samples"]), 3)

    def test_late_delivery_is_ordered_by_measurement_time(self):
        self.start()
        readings = fixture("health.json")["stable"]
        job = self.store.record_samples(self.execution["action_id"], list(reversed(readings)), now=NOW + timedelta(seconds=30))
        self.assertEqual(job["samples"], readings)

    def test_conflicting_timestamp_rolls_back_entire_batch(self):
        self.start()
        readings = fixture("health.json")["stable"]
        self.store.record_samples(self.execution["action_id"], readings[:1], now=NOW + timedelta(seconds=30))
        changed = dict(readings[0], http_status=503)
        with self.assertRaises(VerifierInputError):
            self.store.record_samples(self.execution["action_id"], [readings[1], changed], now=NOW + timedelta(seconds=30))
        self.assertEqual(self.store.get(self.execution["action_id"])["samples"], readings[:1])

    def test_invalid_identity_or_timestamp_never_enters_checkpoint(self):
        self.start()
        reading = fixture("health.json")["stable"][0]
        for change in ({"service": "other"}, {"target": dict(self.execution["target"], uid="other")},
                       {"at": "2026-10-10T12:00:40Z"}, {"at": "2026-10-10T12:02:00Z"}, {"at": "bad"}):
            with self.subTest(change=change), self.assertRaises(VerifierInputError):
                self.store.record_samples(self.execution["action_id"], [dict(reading, **change)], now=NOW + timedelta(seconds=30))
        self.assertEqual(self.store.get(self.execution["action_id"])["samples"], [])

    def test_samples_survive_reopening(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "monitor.sqlite3"
            first = VerificationStore(path)
            readings = fixture("health.json")["stable"][:2]
            try:
                first.start(self.execution, self.policy, now=NOW)
                first.record_samples(self.execution["action_id"], readings, now=NOW + timedelta(seconds=20))
            finally:
                first.close()
            second = VerificationStore(path)
            try:
                self.assertEqual(second.get(self.execution["action_id"])["samples"], readings)
            finally:
                second.close()

    def test_malformed_batch_is_rejected(self):
        self.start()
        for samples in ({}, [None]):
            with self.subTest(samples=samples), self.assertRaises(VerifierInputError):
                self.store.record_samples(self.execution["action_id"], samples, now=NOW)

    def test_partial_window_remains_pending_until_timeout(self):
        self.start()
        self.store.record_samples(self.execution["action_id"], fixture("health.json")["stable"][:1], now=NOW + timedelta(seconds=10))
        self.assertIsNone(self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=10)))
        outcome = self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=65))
        self.assertEqual(outcome["status"], "UNKNOWN")

    def test_success_is_frozen_and_replayed_without_new_timestamps(self):
        self.start()
        self.store.record_samples(self.execution["action_id"], fixture("health.json")["stable"], now=NOW + timedelta(seconds=30))
        outcome = self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=30))
        self.assertEqual(outcome["status"], "RECOVERED")
        self.store.record_samples(self.execution["action_id"], fixture("health.json")["unstable"], now=NOW + timedelta(seconds=31))
        self.assertEqual(self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=90)), outcome)

    def test_unhealthy_reading_finishes_without_waiting_for_timeout(self):
        self.start()
        readings = fixture("health.json")["unstable"][:2]
        self.store.record_samples(self.execution["action_id"], readings, now=NOW + timedelta(seconds=20))
        self.assertEqual(self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=20))["status"], "NOT_RECOVERED")

    def test_no_data_or_late_data_cannot_produce_recovery(self):
        self.start()
        expired = NOW + timedelta(seconds=65)
        self.store.record_samples(self.execution["action_id"], fixture("health.json")["stable"], now=expired)
        self.assertEqual(self.store.get(self.execution["action_id"])["samples"], [])
        self.assertEqual(self.store.evaluate(self.execution["action_id"], now=expired)["status"], "UNKNOWN")

    def test_outcome_acknowledgment_requires_exact_saved_payload(self):
        self.start()
        outcome = self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=65))
        self.assertEqual(self.store.pending_outcomes(), [outcome])
        with self.assertRaises(VerifierInputError):
            self.store.acknowledge_outcome(dict(outcome, status="RECOVERED"))
        self.assertEqual(self.store.pending_outcomes(), [outcome])
        self.store.acknowledge_outcome(outcome)
        self.store.acknowledge_outcome(outcome)
        self.assertEqual(self.store.pending_outcomes(), [])

    def test_unknown_policy_data_waits_then_reports_unknown(self):
        self.start()
        readings = fixture("health.json")["stable"]
        readings[1]["dependencies"] = {}
        self.store.record_samples(self.execution["action_id"], readings, now=NOW + timedelta(seconds=30))
        self.assertIsNone(self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=30)))
        self.assertEqual(self.store.evaluate(self.execution["action_id"], now=NOW + timedelta(seconds=65))["status"], "UNKNOWN")

    # Convergence grace. The execution is recorded at 12:00:45Z (NOW); a 30-second
    # grace ends at 12:01:15Z and its default deadline is 12:02:20Z.

    def graced(self, seconds=30, **health):
        policy = deepcopy(self.policy)
        policy["health"].update(health, convergence_grace_seconds=seconds)
        return policy

    def graced_job(self, path=":memory:", **health):
        """A separate ledger whose frozen policy opts in to the grace."""
        store = VerificationStore(path)
        self.addCleanup(store.close)
        return store, store.start(self.execution, self.graced(**health), now=NOW)

    def reading(self, at, **changes):
        return dict(fixture("health.json")["stable"][0], at="2026-10-10T12:" + at + "Z", **changes)

    def unhealthy(self, at):
        return self.reading(at, dependencies={"mock-db": False})

    def offer(self, store, reading):
        """Checkpoint one reading and evaluate at its own measurement time."""
        at = datetime.fromisoformat(reading["at"].replace("Z", "+00:00"))
        store.record_samples(self.execution["action_id"], [reading], now=at)
        return store.evaluate(self.execution["action_id"], now=at)

    def test_grace_extends_default_deadline_and_must_fit_timeout(self):
        self.assertEqual(self.start()["deadline"], "2026-10-10T12:01:50Z")
        self.assertEqual(self.graced_job()[1]["deadline"], "2026-10-10T12:02:20Z")
        self.assertEqual(self.graced_job(verification_timeout_seconds=50)[1]["deadline"], "2026-10-10T12:01:35Z")
        for timeout in (49, True, 86401):
            with self.subTest(timeout=timeout), self.assertRaises(VerifierInputError):
                self.graced_job(verification_timeout_seconds=timeout)
        with self.assertRaises(VerifierInputError):
            self.graced_job(seconds=601)

    def test_in_grace_unhealthy_reading_keeps_job_open_then_recovers(self):
        store, _ = self.graced_job()
        action = self.execution["action_id"]
        stable = fixture("health.json")["stable"]
        self.assertIsNone(self.offer(store, self.unhealthy("00:47")))
        self.assertIsNone(store.get(action)["outcome"])
        self.assertEqual(store.pending_outcomes(), [])
        self.assertIsNone(self.offer(store, stable[0]))
        self.assertIsNone(self.offer(store, stable[1]))
        outcome = self.offer(store, stable[2])
        self.assertEqual(outcome["status"], "RECOVERED")
        self.assertTrue(all(check["passed"] for check in outcome["checks"]))
        self.assertTrue(outcome["checks"][-1]["detail"].startswith("Excluded 1 unhealthy reading(s)"))
        self.assertEqual(outcome["window_start"], "2026-10-10T12:00:50Z")
        self.assertEqual(store.pending_outcomes(), [outcome])
        self.assertEqual(self.offer(store, self.unhealthy("01:12")), outcome)
        self.assertEqual(store.evaluate(action, now=NOW + timedelta(seconds=200)), outcome)
        job = store.get(action)
        self.assertEqual(job["outcome"], outcome)
        self.assertEqual(job["samples"], [self.unhealthy("00:47")] + stable)

    def test_never_healthy_job_ends_not_recovered(self):
        action = self.execution["action_id"]
        deadline = NOW + timedelta(seconds=95)
        with self.subTest("unhealthy past the grace"):
            store, job = self.graced_job()
            for at in ("00:50", "01:00", "01:10", "01:15"):
                self.assertIsNone(self.offer(store, self.unhealthy(at)))
            outcome = self.offer(store, self.unhealthy("01:16"))
            self.assertEqual(outcome["status"], "NOT_RECOVERED")
            self.assertLess(outcome["window_end"], job["deadline"])
            self.assertFalse(outcome["checks"][-1]["passed"])
            self.assertTrue(outcome["checks"][-1]["detail"].startswith("Counted 4 unhealthy reading(s)"))
        with self.subTest("silence after one unhealthy reading"):
            store, job = self.graced_job()
            self.assertEqual(job["deadline"], "2026-10-10T12:02:20Z")
            self.assertIsNone(self.offer(store, self.unhealthy("00:47")))
            self.assertIsNone(store.evaluate(action, now=deadline - timedelta(seconds=1)))
            outcome = store.evaluate(action, now=deadline)
            self.assertEqual(outcome["status"], "NOT_RECOVERED")
            self.assertFalse(outcome["checks"][-1]["passed"])
            self.assertTrue(outcome["checks"][-1]["detail"].startswith("Counted 1 unhealthy reading(s)"))
        with self.subTest("healthy window completed too late"):
            store, _ = self.graced_job()
            stable = fixture("health.json")["stable"]
            for row in (self.unhealthy("00:47"), *stable[:2]):
                self.assertIsNone(self.offer(store, row))
            store.record_samples(action, [self.reading("02:19")], now=deadline)
            self.assertEqual(len(store.get(action)["samples"]), 3)
            outcome = store.evaluate(action, now=deadline)
            self.assertEqual(outcome["status"], "NOT_RECOVERED")
            self.assertTrue(outcome["checks"][-1]["detail"].startswith("Counted 1 unhealthy reading(s)"))
            self.assertIn("dependency_health", {check["name"] for check in outcome["checks"] if not check["passed"]})
        with self.subTest("only incomplete readings"):
            store, _ = self.graced_job()
            for at in ("00:50", "01:00", "01:10"):
                self.assertIsNone(self.offer(store, self.reading(at, http_status=None)))
            self.assertIsNone(store.evaluate(action, now=deadline - timedelta(seconds=1)))
            outcome = store.evaluate(action, now=deadline)
            self.assertEqual(outcome["status"], "UNKNOWN")
            self.assertTrue(outcome["checks"][-1]["detail"].startswith("No reading was excluded"))

    def test_deferred_job_survives_reopening(self):
        action = self.execution["action_id"]
        stable = fixture("health.json")["stable"]
        rows = [self.unhealthy("00:47")] + stable
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "verifier.sqlite3"
            first, saved = self.graced_job(path)
            self.assertIsNone(self.offer(first, rows[0]))
            first.close()
            second = VerificationStore(path)
            try:
                job = second.get(action)
                self.assertEqual((job["deadline"], job["policy"], job["samples"], job["outcome"]),
                                 (saved["deadline"], self.graced(), rows[:1], None))
                with self.assertRaises(VerifierInputError):
                    second.start(self.execution, self.graced(seconds=31), now=NOW)
                with self.assertRaises(VerifierInputError):
                    second.start(self.execution, self.policy, now=NOW)
                self.assertEqual(second.start(self.execution, self.graced(), now=NOW + timedelta(seconds=40)), job)
                self.assertIsNone(self.offer(second, rows[1]))
                self.assertIsNone(self.offer(second, rows[2]))
                outcome = self.offer(second, rows[3])
            finally:
                second.close()
            self.assertEqual(outcome["status"], "RECOVERED")
            self.assertEqual(outcome["checks"][-1]["detail"],
                             "Excluded 1 unhealthy reading(s) from 2026-10-10T12:00:47Z to 2026-10-10T12:00:47Z, "
                             "within 30 seconds of execution (dependency mock-db); "
                             "sustained health was assessed only on the readings after them")
            third = VerificationStore(path)
            try:
                self.assertEqual(third.get(action)["outcome"], outcome)
                self.assertEqual(third.evaluate(action, now=NOW + timedelta(seconds=500)), outcome)
                self.assertEqual(third.get(action)["deadline"], saved["deadline"])
            finally:
                third.close()
        # Without the key the strict fast path is unchanged.
        self.start()
        self.store.record_samples(action, fixture("health.json")["unstable"][:2], now=NOW + timedelta(seconds=20))
        strict = self.store.evaluate(action, now=NOW + timedelta(seconds=20))
        self.assertEqual(strict["status"], "NOT_RECOVERED")
        self.assertNotIn("convergence", [check["name"] for check in strict["checks"]])

    def test_unhealthy_start_with_uncovered_dependency_stays_unknown(self):
        # The first reading names a dependency the healthy readings never report,
        # so setting it aside would hide missing evidence, not a settled failure.
        store, job = self.graced_job()
        action = self.execution["action_id"]
        first = self.reading("00:47", dependencies={"mock-db": True, "cache": False})
        for row in (first, *fixture("health.json")["stable"]):
            self.assertIsNone(self.offer(store, row))
        self.assertEqual(len(store.get(action)["samples"]), 4)
        deadline = datetime.fromisoformat(job["deadline"].replace("Z", "+00:00"))
        self.assertIsNone(store.evaluate(action, now=deadline - timedelta(seconds=1)))
        outcome = store.evaluate(action, now=deadline)
        self.assertEqual(outcome["status"], "UNKNOWN")
        self.assertTrue(outcome["checks"][-1]["detail"].startswith("No reading was excluded"))
        self.assertIn("dependency_health", {check["name"] for check in outcome["checks"] if not check["passed"]})
