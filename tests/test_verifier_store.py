from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from services.verifier import VerifierInputError
from services.verifier_store import VerificationStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 45, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((ROOT / "samples" / name).read_text(encoding="utf-8"))


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
        for status in ("FAILED", "CONFLICT", "NEEDS_RECONCILIATION"):
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
