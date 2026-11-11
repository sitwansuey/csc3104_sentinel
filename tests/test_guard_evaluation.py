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

from scripts import evaluate_guard as evaluation
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
        for path in (evaluation.DEFAULT_CASES, evaluation.ROOT / "scripts/evaluate_guard.py",
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
                         "--output", str(destination), "--allow-dirty"]
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


REVISION = "0123456789abcdef0123456789abcdef01234567"
CLEAN = {"revision": REVISION, "working_tree_dirty": False}


class ReleaseStatusTests(unittest.TestCase):
    """Issue #63 freeze conditions decide the label; nothing is frozen by default."""

    def manifest(self, directory, **changes):
        value = {"tag": "v1.0.0", "commit": REVISION, "accepted_issue": 63, "accepted_at": "2026-11-20T09:00:00Z"}
        value.update(changes)
        path = Path(directory) / "release-manifest.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_no_manifest_is_preliminary_with_its_reason(self):
        status = evaluation.release_status(CLEAN)
        self.assertEqual((status["status"], status["reason"]), ("PRELIMINARY_NOT_FROZEN", "no_manifest"))
        self.assertIn("#63", status["release_prerequisite"])
        self.assertIn("no_manifest", status["release_prerequisite"])
        self.assertIsNone(status["manifest"])

    def test_commit_mismatch_and_dirty_tree_stay_preliminary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(directory)
            mismatch = evaluation.release_status({"revision": "f" * 40, "working_tree_dirty": False}, path)
            dirty = evaluation.release_status({"revision": REVISION, "working_tree_dirty": True}, path)
            unknown_tree = evaluation.release_status({"revision": REVISION}, path)
            unknown_revision = evaluation.release_status({"revision": "unknown", "working_tree_dirty": False}, path)
        self.assertEqual((mismatch["status"], mismatch["reason"]), ("PRELIMINARY_NOT_FROZEN", "commit_mismatch"))
        self.assertEqual((dirty["status"], dirty["reason"]), ("PRELIMINARY_NOT_FROZEN", "dirty_tree"))
        self.assertEqual(unknown_tree["reason"], "dirty_tree")
        self.assertEqual(unknown_revision["reason"], "revision_unknown")

    def test_matching_clean_commit_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            status = evaluation.release_status(CLEAN, self.manifest(directory))
        self.assertEqual((status["status"], status["reason"]), ("FROZEN_RELEASE", None))
        self.assertEqual(status["manifest"]["tag"], "v1.0.0")
        self.assertIn("v1.0.0", status["release_prerequisite"])

    def test_malformed_manifest_raises(self):
        bad = ({"commit": None}, {"commit": "ABC"}, {"commit": "g" * 40}, {"tag": ""}, {"accepted_issue": 17},
               {"accepted_issue": "63"}, {"accepted_at": "yesterday"}, {"accepted_at": "2026-11-20T09:00:00+08:00"})
        with tempfile.TemporaryDirectory() as directory:
            for changes in bad:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    evaluation.release_status(CLEAN, self.manifest(directory, **changes))
            missing = Path(directory) / "missing-commit.json"
            missing.write_text(json.dumps({"tag": "v1", "accepted_issue": 63, "accepted_at": "2026-11-20T09:00:00Z"}),
                               encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluation.release_status(CLEAN, missing)
            not_json = Path(directory) / "not-json.json"
            not_json.write_text("{tag: v1", encoding="utf-8")
            with self.assertRaises(ValueError):
                evaluation.release_status(CLEAN, not_json)

    def test_malformed_manifest_exits_nonzero_from_the_command_line(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(directory, commit="not-a-commit")
            arguments = ["evaluate_guard.py", "--repeats", "1", "--warmups", "0", "--release-manifest", str(path)]
            with patch.object(sys, "argv", arguments), redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()), patch.object(evaluation, "provenance", return_value=CLEAN):
                with self.assertRaises(SystemExit) as rejected:
                    evaluation.main()
        self.assertEqual(rejected.exception.code, 2)

    def test_run_evaluation_records_the_computed_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.manifest(directory)
            with patch.object(evaluation, "provenance", return_value=CLEAN):
                frozen = evaluation.run_evaluation(repeats=1, warmups=0, release_manifest=path)
                preliminary = evaluation.run_evaluation(repeats=1, warmups=0)
        self.assertEqual(frozen["status"], "FROZEN_RELEASE")
        self.assertEqual(frozen["release"]["manifest"]["commit"], REVISION)
        self.assertIn("frozen release", evaluation.markdown_report(frozen))
        self.assertEqual(preliminary["status"], "PRELIMINARY_NOT_FROZEN")
        self.assertEqual(preliminary["release"]["reason"], "no_manifest")
        self.assertIn("(preliminary)", evaluation.markdown_report(preliminary))


class NoValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(evaluation, "provenance", return_value={"test_run": True}):
            cls.result = evaluation.run_evaluation(repeats=1, warmups=0)

    def test_every_case_is_allowed_by_definition(self):
        configuration = self.result["configurations"]["no_validation"]
        summary = configuration["summary"]
        self.assertEqual(configuration["basis"], "definitional")
        self.assertEqual(configuration["enabled_gates"], [])
        self.assertEqual(summary["unsafe_acceptance"]["numerator"], summary["unsafe_cases"])
        self.assertEqual(summary["unsafe_cases"], 27)
        self.assertEqual(summary["safe_acceptance"]["numerator"], summary["safe_cases"])
        self.assertEqual(summary["errors"], 0)
        self.assertTrue(all(row["verdict"] == "ALLOW" for row in configuration["cases"]))
        self.assertFalse(any(row["verdict"] == "ERROR" for row in configuration["cases"]))
        self.assertTrue(self.result["expectations_met"])

    def test_measured_rows_keep_their_results(self):
        configurations = self.result["configurations"]
        self.assertEqual(configurations["full_available"]["basis"], "measured")
        self.assertEqual(configurations["state_policy_only"]["basis"], "measured")
        self.assertEqual(configurations["full_available"]["summary"]["unsafe_acceptance"]["numerator"], 0)
        self.assertEqual(configurations["state_policy_only"]["summary"]["unsafe_acceptance"]["numerator"], 21)

    def test_markdown_labels_the_row_definitional(self):
        report = evaluation.markdown_report(self.result)
        row = next(line for line in report.splitlines() if line.startswith("| no_validation |"))
        self.assertIn("| definitional |", row)
        self.assertIn("| 27/27 |", row)
        self.assertIn("not a measurement", report)


class DirtyTreeExportTests(unittest.TestCase):
    def export(self, destination, dirty, *extra):
        arguments = ["evaluate_guard.py", "--repeats", "1", "--warmups", "0", "--output", str(destination), *extra]
        record = {"revision": REVISION, "working_tree_dirty": dirty}
        with patch.object(sys, "argv", arguments), patch.object(evaluation, "provenance", return_value=record), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return evaluation.main()

    def test_dirty_tree_refuses_output_and_writes_nothing(self):
        for dirty in (True, None):
            with self.subTest(dirty=dirty), tempfile.TemporaryDirectory() as directory:
                destination = Path(directory) / "results"
                with self.assertRaises(SystemExit) as rejected:
                    self.export(destination, dirty)
                self.assertEqual(rejected.exception.code, 2)
                self.assertFalse(destination.exists())

    def test_clean_tree_or_explicit_override_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self.export(Path(directory) / "clean", False), 0)
            self.assertTrue((Path(directory) / "clean/results.json").exists())
            self.assertEqual(self.export(Path(directory) / "override", True, "--allow-dirty"), 0)
            exported = json.loads((Path(directory) / "override/results.json").read_text(encoding="utf-8"))
            self.assertTrue(exported["provenance"]["working_tree_dirty"])
            self.assertEqual(exported["status"], "PRELIMINARY_NOT_FROZEN")


class VerdictDifferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(evaluation, "provenance", return_value={"test_run": True}):
            cls.result = evaluation.run_evaluation(repeats=1, warmups=0)

    def test_identical_results_have_no_differences_and_timings_are_ignored(self):
        other = deepcopy(self.result)
        for configuration in other["configurations"].values():
            for row in configuration["cases"]:
                row["latency"]["median_ms"] += 1
        self.assertEqual(evaluation.verdict_differences(self.result, other), [])

    def test_changed_verdict_reason_suite_and_cases_are_named(self):
        changed = deepcopy(self.result)
        row = changed["configurations"]["full_available"]["cases"][0]
        row["verdict"] = "ALLOW" if row["verdict"] != "ALLOW" else "REJECT"
        changed["configurations"]["state_policy_only"]["cases"][1]["reason_codes"] = ["SOMETHING_ELSE"]
        changed["configurations"]["no_validation"]["cases"].pop()
        changed["suite_sha256"] = "0" * 64
        problems = evaluation.verdict_differences(changed, self.result)
        self.assertIn("suite_sha256 differs: the case suite changed", problems)
        self.assertTrue(any(line.startswith("full_available/" + row["case_id"] + ":") for line in problems))
        self.assertTrue(any(line.startswith("state_policy_only/") for line in problems))
        self.assertTrue(any("only in the current run" in line for line in problems))
        removed = deepcopy(self.result)
        del removed["configurations"]["no_validation"]
        self.assertTrue(evaluation.verdict_differences(removed, self.result)[0].startswith("configurations differ"))


if __name__ == "__main__":
    unittest.main()
