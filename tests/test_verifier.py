import json
import unittest
from copy import deepcopy
from itertools import product
from pathlib import Path

from services.verifier.core import VerifierInputError, verify
from shared.contracts import validate_message

ROOT = Path(__file__).resolve().parents[1]


def load_sample(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text())


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
        for status in ("FAILED", "CONFLICT", "SUPERSEDED", "NEEDS_RECONCILIATION"):
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

    # Convergence grace: opt-in, leading readings only, never a substitute for health.
    # The fixture execution is recorded at 12:00:45Z; the stable window is :50, :00, :10.

    def graced(self, seconds=60):
        policy = deepcopy(self.policy)
        policy["health"]["convergence_grace_seconds"] = seconds
        return policy

    def reading(self, at, **changes):
        row = deepcopy(self.samples[0])
        row.update(changes, at="2026-10-10T12:" + at + "Z")
        return row

    def unhealthy(self, at):
        return self.reading(at, dependencies={"mock-db": False})

    def convergence(self, result):
        self.assertEqual([check["name"] for check in result["checks"]].count("convergence"), 1)
        self.assertEqual(result["checks"][-1]["name"], "convergence")
        validate_message(result)
        return result["checks"][-1]

    def failed(self, result):
        return {check["name"] for check in result["checks"] if not check["passed"]}

    def test_policy_without_grace_is_unchanged(self):
        health = load_sample("health.json")
        names = ["execution", "sample_count", "sample_identity", "sample_timestamps",
                 "stable_window", "sample_gaps", "service_health", "dependency_health"]
        for samples, status in ((health["stable"], "RECOVERED"), (health["unstable"], "NOT_RECOVERED"),
                                (health["stable"][:1], "UNKNOWN"), ([], "UNKNOWN")):
            with self.subTest(count=len(samples), status=status):
                result = verify(self.execution, samples, self.policy)
                self.assertEqual(verify(self.execution, samples, self.policy, final=False), result)
                self.assertEqual(result["status"], status)
                self.assertEqual([check["name"] for check in result["checks"]], names)

    def test_invalid_grace_or_final_raises(self):
        for value in (True, 0, -1, "60", 1.5, None, 601):
            with self.subTest(value=value), self.assertRaises(VerifierInputError):
                verify(self.execution, self.samples, self.graced(value))
        for value in (1, 600):
            with self.subTest(value=value):
                self.assertEqual(verify(self.execution, self.samples, self.graced(value))["status"], "RECOVERED")
        with self.assertRaises(VerifierInputError):
            verify(self.execution, self.samples, self.policy, final="yes")

    def test_leading_in_grace_unhealthy_reading_is_excluded_and_disclosed(self):
        policy = self.graced(60)
        for change, cause in (({"dependencies": {"mock-db": False}}, "(dependency mock-db)"),
                              ({"http_status": 503}, "(service)"), ({"ready_replicas": 0}, "(service)")):
            with self.subTest(change=change):
                rows = [self.reading("00:47", **change)] + self.samples
                original = deepcopy((self.execution, rows, policy))
                result = verify(self.execution, rows, policy)
                self.assertEqual((self.execution, rows, policy), original)
                check = self.convergence(result)
                self.assertEqual(result["status"], "RECOVERED")
                self.assertTrue(all(item["passed"] for item in result["checks"]))
                self.assertEqual(result["window_start"], "2026-10-10T12:00:50Z")
                self.assertEqual(result["window_end"], "2026-10-10T12:01:10Z")
                count = next(item for item in result["checks"] if item["name"] == "sample_count")
                self.assertTrue(count["detail"].startswith("Received 3 readings"))
                self.assertTrue(check["passed"])
                self.assertTrue(check["detail"].startswith("Excluded 1 unhealthy reading(s)"))
                self.assertIn("within 60 seconds", check["detail"])
                self.assertIn(cause, check["detail"])
                # A ledger that is still collecting reaches the same verdict.
                self.assertEqual(verify(self.execution, rows, policy, final=False), result)

    def test_grace_boundary_is_inclusive_from_execution(self):
        policy = self.graced(2)
        result = verify(self.execution, [self.unhealthy("00:47")] + self.samples, policy)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertTrue(self.convergence(result)["detail"].startswith("Excluded 1 "))
        rows = [self.unhealthy("00:48")] + self.samples
        for final in (True, False):
            with self.subTest(final=final):
                result = verify(self.execution, rows, policy, final=final)
                check = self.convergence(result)
                self.assertEqual(result["status"], "NOT_RECOVERED")
                self.assertTrue(check["passed"])
                self.assertTrue(check["detail"].startswith("No reading was excluded"))
                self.assertEqual(self.failed(result), {"dependency_health"})

    def test_relapse_after_a_healthy_reading_is_never_tolerated(self):
        policy = self.graced(60)
        for change, name in (({"dependencies": {"mock-db": False}}, "dependency_health"),
                             ({"http_status": 503}, "service_health")):
            bad = lambda at: self.reading(at, **change)
            for label, rows in (("H,U", [self.samples[0], bad("00:55")]),
                                ("U,H,U", [bad("00:47"), self.samples[0], bad("00:55")]),
                                ("U,H,H,H,U", [bad("00:47")] + self.samples + [bad("01:15")])):
                for final in (True, False):
                    with self.subTest(name=name, rows=label, final=final):
                        result = verify(self.execution, rows, policy, final=final)
                        check = self.convergence(result)
                        self.assertEqual(result["status"], "NOT_RECOVERED")
                        self.assertIn(name, self.failed(result))
                        count = next(item for item in result["checks"] if item["name"] == "sample_count")
                        self.assertTrue(count["detail"].startswith(f"Received {len(rows)} readings"))
                        self.assertEqual(result["window_start"], rows[0]["at"])
                        if label.startswith("U"):
                            self.assertFalse(check["passed"])
                            self.assertTrue(check["detail"].startswith("Counted 1 unhealthy reading(s)"))
                        else:
                            self.assertTrue(check["passed"])
                            self.assertTrue(check["detail"].startswith("No reading was excluded"))

    def test_final_flag_only_defers_a_tolerated_start(self):
        policy = self.graced(60)
        for rows in ([self.unhealthy("00:47")], [self.unhealthy("00:47")] + self.samples[:2]):
            with self.subTest(count=len(rows)):
                deferred = verify(self.execution, rows, policy, final=False)
                check = self.convergence(deferred)
                self.assertEqual(deferred["status"], "UNKNOWN")
                self.assertFalse(check["passed"])
                self.assertTrue(check["detail"].startswith("Deferred 1 unhealthy reading(s)"))
                counted = verify(self.execution, rows, policy)
                check = self.convergence(counted)
                self.assertEqual(counted["status"], "NOT_RECOVERED")
                self.assertFalse(check["passed"])
                self.assertTrue(check["detail"].startswith("Counted 1 unhealthy reading(s)"))
                # Apart from the disclosure, the final verdict is the strict one over every reading.
                self.assertEqual(counted["checks"][:-1], verify(self.execution, rows, self.policy)["checks"])
                self.assertEqual(deferred["checks"][:-1], counted["checks"][:-1])
                count = next(item for item in counted["checks"] if item["name"] == "sample_count")
                self.assertTrue(count["detail"].startswith(f"Received {len(rows)} readings"))
                self.assertIn("dependency_health", self.failed(counted))
        for final in (True, False):
            with self.subTest(final=final):
                result = verify(self.execution, self.samples[:2], policy, final=final)
                check = self.convergence(result)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertTrue(check["passed"])
                self.assertTrue(check["detail"].startswith("No reading was excluded"))

    def test_grace_never_shortens_or_gaps_the_healthy_window(self):
        policy = self.graced(60)
        for label, times in (("short", ("00:50", "00:55", "01:00")), ("gapped", ("00:50", "01:00", "01:20"))):
            rows = [self.unhealthy("00:47")] + [self.reading(at) for at in times]
            self.assertEqual(verify(self.execution, rows[1:], self.policy)["status"], "UNKNOWN")
            for final, expected in ((False, "UNKNOWN"), (True, "NOT_RECOVERED")):
                with self.subTest(window=label, final=final):
                    result = verify(self.execution, rows, policy, final=final)
                    self.assertEqual(result["status"], expected)
                    self.assertFalse(self.convergence(result)["passed"])

    def test_incomplete_or_untrusted_first_reading_is_not_tolerated(self):
        policy = self.graced(60)
        for change in ({"http_status": None}, {"dependencies": {"mock-db": None}}, {"service": "other"},
                       {"target": dict(self.execution["target"], uid="recreated")}):
            for final in (True, False):
                with self.subTest(change=change, final=final):
                    rows = [self.reading("00:47", **change)] + self.samples
                    result = verify(self.execution, rows, policy, final=final)
                    check = self.convergence(result)
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertTrue(check["detail"].startswith("No reading was excluded"))
                    self.assertEqual(result["checks"][:-1], verify(self.execution, rows, self.policy)["checks"])

    def test_start_is_judged_only_when_the_whole_window_is_trusted(self):
        # Each first reading is tolerable when judged alone and the three stable
        # readings qualify alone; together the evidence is incomplete or unordered.
        policy = self.graced(60)
        self.assertEqual(verify(self.execution, self.samples, self.policy)["status"], "RECOVERED")
        for label, first in (
                ("dependency the healthy readings never report",
                 self.reading("00:47", dependencies={"mock-db": True, "cache": False})),
                ("stamped after the first healthy reading", self.unhealthy("00:55")),
                ("same stamp as the first healthy reading", self.unhealthy("00:50"))):
            rows = [first] + self.samples
            self.assertEqual(verify(self.execution, [first], self.policy)["status"], "NOT_RECOVERED", label)
            for final in (True, False):
                with self.subTest(first=label, final=final):
                    result = verify(self.execution, rows, policy, final=final)
                    check = self.convergence(result)
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertTrue(check["passed"])
                    self.assertTrue(check["detail"].startswith("No reading was excluded"))
                    self.assertEqual(result["checks"][:-1], verify(self.execution, rows, self.policy)["checks"])

    def test_disclosure_names_at_most_eight_causes(self):
        names = [f"dep-{index:02d}" for index in range(11)]
        policy = self.graced(60)
        policy["health"]["required_dependencies"] = names
        healthy = [dict(row, dependencies=dict.fromkeys(names, True)) for row in self.samples]
        for failing, service, listed, more in ((names, 503, names[:8], ", +4 more"),
                                               (names[:9], 200, names[:8], ", +1 more"),
                                               (names[:7], 503, names[:7] + [None], "")):
            with self.subTest(failing=len(failing), service=service):
                first = self.reading("00:47", http_status=service,
                                     dependencies={name: name not in failing for name in names})
                result = verify(self.execution, [first] + healthy, policy)
                self.assertEqual(result["status"], "RECOVERED")
                causes = ", ".join("dependency " + name if name else "service" for name in listed)
                self.assertIn("within 60 seconds of execution (" + causes + more + "); ",
                              self.convergence(result)["detail"])

    def test_tolerance_only_turns_a_qualifying_suffix_into_recovery(self):
        kinds = {"H": {}, "D": {"dependencies": {"mock-db": False}}, "S": {"http_status": 503},
                 "I": {"http_status": None}}
        sequences = ["".join(items) for length in range(6) for items in product(kinds, repeat=length)]
        self.assertEqual(len(sequences), 1365)
        tolerated = 0
        for seconds in (15, 60):  # Readings are 5, 15, 25, 35 and 45 seconds after execution.
            policy = self.graced(seconds)
            for sequence in sequences:
                rows = [self.reading(f"{(50 + 10 * index) // 60:02d}:{(50 + 10 * index) % 60:02d}", **kinds[kind])
                        for index, kind in enumerate(sequence)]
                strict = verify(self.execution, rows, self.policy)["status"]
                final = verify(self.execution, rows, policy)
                interim = verify(self.execution, rows, policy, final=False)["status"]
                self.assertIn(interim, {"UNKNOWN", final["status"]}, sequence)
                if final["status"] == strict:
                    continue
                tolerated += 1
                lead = next(index for index, kind in enumerate(sequence + "H")
                            if kind not in "DS" or 5 + 10 * index > seconds)
                self.assertEqual((strict, final["status"]), ("NOT_RECOVERED", "RECOVERED"), sequence)
                self.assertGreater(lead, 0, sequence)
                self.assertEqual(verify(self.execution, rows[lead:], self.policy)["status"], "RECOVERED", sequence)
                self.assertTrue(all(check["passed"] for check in final["checks"]), sequence)
                self.assertEqual(final["window_start"], rows[lead]["at"], sequence)
                self.assertTrue(final["checks"][-1]["detail"].startswith(f"Excluded {lead} "), sequence)
        self.assertGreater(tolerated, 0)
