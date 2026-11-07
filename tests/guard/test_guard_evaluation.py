"""Offline Guard comparisons retain independent cases and honest error counts."""

from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.evaluation import evaluate_guard as evaluation
from services.guard.common import json_sha256


class GuardEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.suite, cls.materialized = evaluation.load_cases(evaluation.DEFAULT_CASES)
        with patch.object(evaluation, "provenance", return_value={"test_run": True}):
            cls.result = evaluation.run_evaluation(repeats=2, warmups=1)

    def small_suite(self):
        """One valid case and one independent dependency veto keep fault tests small."""
        cases = [deepcopy(next(item for item in self.materialized
                               if item[0]["id"] == case_id))
                 for case_id in ("safe-approved-rollback", "dependency-blocked")]
        return deepcopy(self.suite), cases

    def run_small(self, evaluator, *, repeats=2, warmups=0):
        with patch.object(evaluation, "load_cases", return_value=self.small_suite()):
            with patch.object(evaluation, "evaluate_configuration", side_effect=evaluator):
                with patch.object(evaluation, "provenance", return_value={"test_run": True}):
                    return evaluation.run_evaluation(repeats=repeats, warmups=warmups)

    def test_repetitions_and_warmups_do_not_inflate_accuracy_denominators(self):
        labels = Counter(specification["label"] for specification, _ in self.materialized)
        case_count = len(self.materialized)
        for name, configuration in self.result["configurations"].items():
            with self.subTest(configuration=name):
                summary = configuration["summary"]
                self.assertEqual(summary["safe_cases"], labels["SAFE"])
                self.assertEqual(summary["unsafe_cases"], labels["UNSAFE"])
                self.assertEqual(len(configuration["cases"]), case_count)
                self.assertEqual(len({row["case_id"] for row in configuration["cases"]}), case_count)
                for label in ("SAFE", "UNSAFE"):
                    self.assertEqual(sum(summary["verdict_counts"][label].values()), labels[label])
                for metric in ("unsafe_action_rejection", "unsafe_non_authorization", "unsafe_acceptance"):
                    self.assertEqual(summary[metric]["denominator"], labels["UNSAFE"])
                for metric in ("false_rejection", "safe_non_authorization", "safe_acceptance"):
                    self.assertEqual(summary[metric]["denominator"], labels["SAFE"])
                self.assertEqual(summary["latency"]["samples"], case_count * 2)
                self.assertTrue(all(row["latency"]["samples"] == 2 for row in configuration["cases"]))

    def test_strict_rejection_excludes_escalation_and_errors(self):
        expected_unsafe = Counter(specification["expected_full"]["verdict"]
                                  for specification, _ in self.materialized
                                  if specification["label"] == "UNSAFE")
        summary = self.result["configurations"]["full_available"]["summary"]
        self.assertGreater(expected_unsafe["REJECT"], 0)
        self.assertGreater(expected_unsafe["ESCALATE"], 0)
        self.assertEqual(summary["unsafe_action_rejection"]["numerator"], expected_unsafe["REJECT"])
        self.assertEqual(summary["unsafe_non_authorization"]["numerator"],
                         expected_unsafe["REJECT"] + expected_unsafe["ESCALATE"])
        self.assertLess(summary["unsafe_action_rejection"]["rate"],
                        summary["unsafe_non_authorization"]["rate"])
        self.assertEqual(summary["unsafe_acceptance"]["numerator"], 0)
        self.assertEqual(summary["false_rejection"]["numerator"], 0)
        self.assertEqual(summary["safe_acceptance"]["rate"], 1)
        self.assertEqual(summary["errors"], 0)

    def test_required_case_families_reach_their_declared_full_gate(self):
        categories = {specification["category"] for specification, _ in self.materialized}
        self.assertTrue({"stale", "forbidden", "tampered", "dependency", "approval"} <= categories)
        actual = {row["case_id"]: row for row in self.result["configurations"]["full_available"]["cases"]}
        for specification, _ in self.materialized:
            with self.subTest(case=specification["id"]):
                expected, row = specification["expected_full"], actual[specification["id"]]
                self.assertEqual(row["verdict"], expected["verdict"])
                self.assertIn(expected["reason"], row["reason_codes"])
                self.assertTrue(row["expectation_met"])
                self.assertTrue(row["consistent_across_repeats"])
        self.assertTrue(self.result["expectations_met"])
        configurations = self.result["configurations"]
        state, full = configurations["state_policy_only"], configurations["full_available"]
        self.assertTrue(set(state["enabled_gates"]) < set(full["enabled_gates"]))
        for name, configuration in configurations.items():
            with self.subTest(configuration=name):
                self.assertFalse(set(configuration["enabled_gates"]) & set(configuration["disabled_gates"]))
                self.assertIn("server_admission_dry_run", configuration["disabled_gates"])
                self.assertIn("runtime_preview", configuration["disabled_gates"])

    def test_comparators_receive_identical_inputs_without_mutating_suite(self):
        suite, cases = deepcopy(self.suite), deepcopy(self.materialized)
        original = deepcopy((suite, cases))
        observed = defaultdict(list)
        actual_evaluator = evaluation.evaluate_configuration

        def observe(name, candidate):
            serialized = deepcopy(candidate)
            serialized["now"] = serialized["now"].isoformat().replace("+00:00", "Z")
            observed[name].append(json_sha256(serialized))
            return actual_evaluator(name, candidate)

        with patch.object(evaluation, "load_cases", return_value=(suite, cases)):
            with patch.object(evaluation, "evaluate_configuration", side_effect=observe):
                with patch.object(evaluation, "provenance", return_value={}):
                    result = evaluation.run_evaluation(repeats=1, warmups=0)
        self.assertEqual((suite, cases), original)
        expected_hashes = [json_sha256(values) for _, values in cases]
        for name in evaluation.CONFIGURATIONS:
            self.assertEqual(observed[name], expected_hashes)
            rows = result["configurations"][name]["cases"]
            self.assertEqual([row["case_inputs_sha256"] for row in rows], expected_hashes)

    def test_exceptions_on_first_or_later_measured_trial_never_earn_refusal_credit(self):
        for failed_trial in (1, 2):
            with self.subTest(failed_trial=failed_trial):
                calls = Counter()

                def fail_trial(name, values):
                    blocked = values["dependencies"]["status"] == "BLOCKED"
                    key = (name, blocked)
                    calls[key] += 1
                    if calls[key] == failed_trial:
                        raise RuntimeError("sensitive input must not appear in the report")
                    return {"verdict": "REJECT" if blocked else "ALLOW",
                            "reason_codes": ["DEPENDENCY_BLOCKED" if blocked else "VALIDATION_PASSED"]}

                result = self.run_small(fail_trial)
                self.assertFalse(result["expectations_met"])
                self.assertNotIn("sensitive input", json.dumps(result))
                for configuration in result["configurations"].values():
                    summary = configuration["summary"]
                    self.assertEqual(summary["errors"], 2)
                    self.assertEqual(summary["unsafe_action_rejection"]["numerator"], 0)
                    self.assertEqual(summary["unsafe_non_authorization"]["numerator"], 0)
                    self.assertEqual(summary["safe_acceptance"]["numerator"], 0)
                    self.assertEqual(summary["unsafe_action_rejection"]["denominator"], 1)
                    self.assertEqual(summary["safe_acceptance"]["denominator"], 1)
                    for row in configuration["cases"]:
                        self.assertEqual(row["verdict"], "ERROR")
                        self.assertEqual(row["trial_verdict_counts"]["ERROR"], 1)
                        self.assertFalse(row["consistent_across_repeats"])

    def test_inconsistent_verdicts_or_reasons_never_earn_refusal_credit(self):
        for change_verdict in (True, False):
            with self.subTest(change_verdict=change_verdict):
                calls = Counter()

                def inconsistent(name, values):
                    key = (name, values["dependencies"]["status"])
                    calls[key] += 1
                    later = calls[key] > 1
                    return {"verdict": "ESCALATE" if change_verdict and later else "REJECT",
                            "reason_codes": ["SECOND_REASON" if later else "FIRST_REASON"]}

                result = self.run_small(inconsistent)
                self.assertFalse(result["expectations_met"])
                for configuration in result["configurations"].values():
                    summary = configuration["summary"]
                    self.assertEqual(summary["errors"], 2)
                    self.assertEqual(summary["unsafe_action_rejection"]["numerator"], 0)
                    self.assertEqual(summary["unsafe_non_authorization"]["numerator"], 0)
                    self.assertTrue(all(row["verdict"] == "ERROR" for row in configuration["cases"]))

    def test_warmups_do_not_count_as_measured_trials(self):
        calls = Counter()

        def warmup_failure(name, values):
            blocked = values["dependencies"]["status"] == "BLOCKED"
            key = (name, blocked)
            calls[key] += 1
            if calls[key] == 1:
                raise RuntimeError("unscored warmup failure")
            return {"verdict": "REJECT" if blocked else "ALLOW",
                    "reason_codes": ["DEPENDENCY_BLOCKED" if blocked else "VALIDATION_PASSED"]}

        result = self.run_small(warmup_failure, repeats=1, warmups=1)
        self.assertTrue(result["expectations_met"])
        for configuration in result["configurations"].values():
            self.assertEqual(configuration["summary"]["errors"], 0)
            self.assertEqual(configuration["summary"]["latency"]["samples"], 2)
            self.assertTrue(all(row["latency"]["samples"] == 1 for row in configuration["cases"]))

    def test_provenance_records_suite_version_and_content_and_source_hashes(self):
        self.assertEqual(self.result["suite_version"], self.suite["suite_version"])
        self.assertEqual(self.result["suite_sha256"], json_sha256(self.suite))
        provenance = evaluation.provenance(evaluation.DEFAULT_CASES)
        for path in (evaluation.DEFAULT_CASES, evaluation.ROOT / "scripts/evaluation/evaluate_guard.py",
                     evaluation.ROOT / "services/guard/core.py", evaluation.ROOT / "shared/contracts.py"):
            self.assertEqual(provenance["source_sha256"][path.relative_to(evaluation.ROOT).as_posix()],
                             hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertRegex(provenance["revision"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(provenance["working_tree_dirty"], bool)
        changed = deepcopy(self.suite)
        changed["cases"][0]["description"] += " Changed."
        self.assertNotEqual(self.result["suite_sha256"], json_sha256(changed))

    def test_external_suite_has_provenance_and_results_remain_preliminary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom-cases.json"
            path.write_text(json.dumps(self.suite), encoding="utf-8")
            provenance = evaluation.provenance(path)
            self.assertEqual(provenance["source_sha256"]["case_suite.json"],
                             hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(self.result["status"], "PRELIMINARY_NOT_FROZEN")
        self.assertEqual(self.result["evidence_kind"], "synthetic_fixture")
        self.assertIn("#63", self.result["release_prerequisite"])
        self.assertFalse(self.result["execution"]["cluster_contacted"])
        self.assertEqual(self.result["execution"]["repair_writes_attempted"], 0)

    def test_suite_loader_rejects_duplicate_ids_bad_labels_and_preview_inputs(self):
        invalid = []
        duplicate = deepcopy(self.suite)
        duplicate["cases"][1]["id"] = duplicate["cases"][0]["id"]
        invalid.append(duplicate)
        bad_label = deepcopy(self.suite)
        bad_label["cases"][0]["label"] = "UNKNOWN"
        invalid.append(bad_label)
        bad_version = deepcopy(self.suite)
        bad_version["schema_version"] = "2.0"
        invalid.append(bad_version)
        live_evidence = deepcopy(self.suite)
        live_evidence["evidence_kind"] = "live"
        invalid.append(live_evidence)
        preview = deepcopy(self.suite)
        preview["base_inputs"]["preview"] = {"status": "PASSED"}
        invalid.append(preview)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            for index, suite in enumerate(invalid):
                with self.subTest(invalid_suite=index):
                    path.write_text(json.dumps(suite), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        evaluation.load_cases(path)

    def test_export_creates_reviewable_files_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "results"
            arguments = ["evaluate_guard.py", "--repeats", "1", "--warmups", "0",
                         "--output", str(destination)]
            with patch.object(sys, "argv", arguments), redirect_stdout(io.StringIO()):
                self.assertEqual(evaluation.main(), 0)
            json_path, markdown_path = destination / "results.json", destination / "README.md"
            before = (json_path.read_bytes(), markdown_path.read_bytes())
            exported = json.loads(before[0])
            self.assertEqual(exported["suite_sha256"], self.result["suite_sha256"])
            self.assertEqual(exported["suite_version"], self.suite["suite_version"])
            self.assertTrue(exported["provenance"]["source_sha256"])
            self.assertIn("synthetic fixtures", before[1].decode("utf-8"))
            self.assertIn("Unsafe ESCALATE", before[1].decode("utf-8"))
            with patch.object(sys, "argv", arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as rejected:
                    evaluation.main()
            self.assertEqual(rejected.exception.code, 2)
            self.assertEqual((json_path.read_bytes(), markdown_path.read_bytes()), before)

    def test_invalid_trial_counts_are_rejected(self):
        for arguments in ({"repeats": 0}, {"repeats": -1}, {"repeats": True},
                          {"repeats": 1.5}, {"warmups": -1}, {"warmups": False}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                evaluation.run_evaluation(**arguments)

    def test_latency_percentile_uses_nearest_rank(self):
        summary = evaluation.latency_summary(list(range(20, 0, -1)))
        self.assertEqual(summary["samples"], 20)
        self.assertEqual(summary["median_ms"], 10.5)
        self.assertEqual(summary["p95_ms"], 19)
        self.assertEqual(summary["min_ms"], 1)
        self.assertEqual(summary["max_ms"], 20)


if __name__ == "__main__":
    unittest.main()
