import json
import unittest
from copy import deepcopy
from pathlib import Path

from services.verifier import VerifierInputError, verify
from shared.contracts import validate_message

ROOT = Path(__file__).resolve().parents[1]


def load_sample(name):
    return json.loads((ROOT / "samples" / name).read_text())


class VerifierTests(unittest.TestCase):
    def setUp(self):
        self.execution = load_sample("messages/execution-result.json")
        self.samples = load_sample("health.json")["stable"]
        self.policy = load_sample("policy.json")

    def outcome(self, samples=None):
        result = verify(self.execution, self.samples if samples is None else samples, self.policy)
        validate_message(result)
        return result

    def test_stable_health_reports_recovered(self):
        result = verify(
            load_sample("messages/execution-result.json"),
            load_sample("health.json")["stable"],
            load_sample("policy.json"),
        )

        validate_message(result)
        self.assertEqual(result["kind"], "outcome")
        self.assertEqual(result["status"], "RECOVERED")

    def test_unstable_readings_do_not_report_recovery(self):
        self.assertEqual(self.outcome(load_sample("health.json")["unstable"])["status"], "NOT_RECOVERED")

    def test_noop_can_be_verified(self):
        self.execution["status"] = "NOOP"
        self.assertEqual(self.outcome()["status"], "RECOVERED")

    def test_empty_and_insufficient_samples_are_unknown(self):
        for samples in ([], self.samples[:1], self.samples[:2]):
            with self.subTest(count=len(samples)):
                self.assertEqual(self.outcome(samples)["status"], "UNKNOWN")

    def test_short_window_is_unknown(self):
        self.samples[-1]["at"] = "2026-10-10T12:01:05Z"
        self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_excessive_gap_is_unknown(self):
        self.samples[-1]["at"] = "2026-10-10T12:01:20Z"
        self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_inclusive_duration_and_gap_boundaries(self):
        self.policy["health"]["max_sample_gap_seconds"] = 10
        self.assertEqual(self.outcome()["status"], "RECOVERED")

    def test_duplicate_out_of_order_and_pre_execution_times_are_unknown(self):
        for times in (
            ["2026-10-10T12:00:50Z"] * 3,
            [row["at"] for row in reversed(self.samples)],
            ["2026-10-10T12:00:40Z", "2026-10-10T12:00:50Z", "2026-10-10T12:01:00Z"],
        ):
            with self.subTest(times=times):
                rows = deepcopy(self.samples)
                for row, at in zip(rows, times):
                    row["at"] = at
                result = self.outcome(rows)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertGreaterEqual(result["window_start"], self.execution["recorded_at"])

    def test_bad_timestamps_are_unknown(self):
        for at in (None, "invalid", "2026-10-10T12:00:50", "2026-10-10T20:00:50+08:00"):
            with self.subTest(at=at):
                rows = deepcopy(self.samples)
                rows[0]["at"] = at
                self.assertEqual(self.outcome(rows)["status"], "UNKNOWN")

    def test_wrong_service_or_full_identity_is_unknown(self):
        self.samples[0]["service"] = "another-service"
        self.assertEqual(self.outcome()["status"], "UNKNOWN")
        self.samples = load_sample("health.json")["stable"]
        self.samples[0]["target"] = dict(self.execution["target"], uid="recreated")
        self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_http_and_readiness_failures_are_not_recovered(self):
        for change in ({"http_status": 503}, {"ready_replicas": 0}):
            with self.subTest(change=change):
                rows = deepcopy(self.samples)
                rows[1].update(change)
                self.assertEqual(self.outcome(rows)["status"], "NOT_RECOVERED")

    def test_missing_or_invalid_health_is_unknown(self):
        for change in ({"http_status": None}, {"ready_replicas": True}, {"desired_replicas": 0}, {"desired_replicas": -1}):
            with self.subTest(change=change):
                rows = deepcopy(self.samples)
                rows[1].update(change)
                self.assertEqual(self.outcome(rows)["status"], "UNKNOWN")

    def test_missing_dependency_evidence_is_unknown(self):
        for value in (None, {}, {"mock-db": None}, {"mock-db": "true"}):
            with self.subTest(value=value):
                rows = deepcopy(self.samples)
                rows[1]["dependencies"] = value
                self.assertEqual(self.outcome(rows)["status"], "UNKNOWN")

    def test_declared_dependency_missing_from_all_samples_is_unknown(self):
        self.policy["health"]["required_dependencies"] = ["another-db"]
        self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_failed_dependency_is_not_recovered(self):
        self.samples[1]["dependencies"]["mock-db"] = False
        self.assertEqual(self.outcome()["status"], "NOT_RECOVERED")

    def test_single_service_without_declared_dependencies(self):
        for row in self.samples:
            row["dependencies"] = {}
        self.assertEqual(self.outcome()["status"], "RECOVERED")

    def test_unapplied_or_uncertain_execution_is_unknown(self):
        for status in ("FAILED", "CONFLICT", "NEEDS_RECONCILIATION"):
            with self.subTest(status=status):
                self.execution["status"] = status
                self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_missing_execution_version_is_unknown(self):
        self.execution["resource_version"] = None
        self.assertEqual(self.outcome()["status"], "UNKNOWN")

    def test_invalid_policy_raises(self):
        for key, value in (("minimum_samples", True), ("stable_window_seconds", 0),
                           ("max_sample_gap_seconds", -1), ("required_http_status", "200"),
                           ("required_dependencies", "mock-db")):
            with self.subTest(key=key):
                rules = deepcopy(self.policy)
                rules["health"][key] = value
                with self.assertRaises(VerifierInputError):
                    verify(self.execution, self.samples, rules)

    def test_wrong_message_kind_and_sample_container_raise(self):
        with self.assertRaises(VerifierInputError):
            verify(load_sample("messages/proposal.json"), self.samples, self.policy)
        with self.assertRaises(VerifierInputError):
            verify(self.execution, {}, self.policy)

    def test_malformed_sample_is_unknown(self):
        self.assertEqual(self.outcome([None, *self.samples])["status"], "UNKNOWN")

    def test_inputs_are_unchanged_and_result_identity_is_copied(self):
        original = deepcopy((self.execution, self.samples, self.policy))
        result = self.outcome()
        self.assertEqual((self.execution, self.samples, self.policy), original)
        result["target"]["uid"] = "changed"
        self.assertEqual(self.execution, original[0])
