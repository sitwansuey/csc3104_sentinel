"""The committed Guard evaluation still matches what the current code and cases produce.

Timings are host-specific and ignored. Verdicts, reason codes and the suite hash
must match a fresh offline run exactly. When this fails, regenerate the result
(see data/experimentation/guard/README.md) and collect it as a new bundle.
"""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_guard as evaluation
from scripts.evidence import summarize

ROOT = Path(__file__).resolve().parents[1]
REGENERATE = ("Regenerate on a clean tree: python scripts/evaluate_guard.py --output <new dir>, then "
              "python scripts/evidence/collect.py --run <new dir> --class guard-eval --bundle-id <id> "
              "and mark the old bundle superseded.")


def newest_guard_results(root=ROOT):
    """Path of results.json in the newest current guard-eval bundle, or None."""
    entries = [entry for entry in summarize.load_index(root)["bundles"]
               if entry["class"] == "guard-eval" and entry.get("status") == "current"]
    if not entries:
        return None
    entry = max(entries, key=lambda item: (item.get("captured_at") or "", item["bundle_id"]))
    return Path(root) / "evidence" / entry["path"] / "results.json"


def fresh_result():
    with patch.object(evaluation, "provenance", return_value={"test_run": True}):
        return evaluation.run_evaluation(repeats=1, warmups=0)


def stale_problems(results_path, current):
    committed = json.loads(Path(results_path).read_text(encoding="utf-8"))
    return evaluation.verdict_differences(committed, current)


class CommittedGuardEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = newest_guard_results()
        cls.current = fresh_result()

    def test_a_guard_evaluation_bundle_is_committed(self):
        self.assertIsNotNone(self.path, "No current guard-eval bundle in evidence/index.json")
        self.assertTrue(self.path.is_file())

    def test_committed_verdicts_match_the_current_code(self):
        if self.path is None:
            self.skipTest("no committed guard-eval bundle")
        problems = stale_problems(self.path, self.current)
        self.assertEqual(problems, [], "Committed Guard evaluation is stale:\n" + "\n".join(problems)
                         + "\n" + REGENERATE)

    def test_committed_result_is_labelled_honestly(self):
        if self.path is None:
            self.skipTest("no committed guard-eval bundle")
        committed = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(committed["evidence_kind"], "synthetic_fixture")
        self.assertIn(committed["status"], (evaluation.FROZEN, evaluation.PRELIMINARY))
        self.assertEqual(committed["release"]["status"], committed["status"])
        self.assertEqual(committed["configurations"]["no_validation"]["basis"], "definitional")
        self.assertIs(committed["provenance"]["working_tree_dirty"], False)
        self.assertTrue(committed["expectations_met"])

    def test_a_mutated_copy_is_reported_as_stale(self):
        if self.path is None:
            self.skipTest("no committed guard-eval bundle")
        committed = json.loads(self.path.read_text(encoding="utf-8"))
        changed = deepcopy(committed)
        row = changed["configurations"]["full_available"]["cases"][0]
        row["verdict"] = "ALLOW" if row["verdict"] != "ALLOW" else "REJECT"
        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / "results.json"
            copy.write_text(json.dumps(changed), encoding="utf-8")
            problems = stale_problems(copy, self.current)
        self.assertEqual(len(problems), 1)
        self.assertTrue(problems[0].startswith("full_available/" + row["case_id"] + ": committed "))

    def test_a_changed_case_suite_is_reported_as_stale(self):
        if self.path is None:
            self.skipTest("no committed guard-eval bundle")
        suite = json.loads(evaluation.DEFAULT_CASES.read_text(encoding="utf-8"))
        suite["cases"][0]["description"] += " Changed."
        with tempfile.TemporaryDirectory() as directory:
            cases = Path(directory) / "cases.json"
            cases.write_text(json.dumps(suite), encoding="utf-8")
            with patch.object(evaluation, "provenance", return_value={"test_run": True}):
                changed = evaluation.run_evaluation(cases, repeats=1, warmups=0)
        self.assertIn("suite_sha256 differs: the case suite changed", stale_problems(self.path, changed))


if __name__ == "__main__":
    unittest.main()
