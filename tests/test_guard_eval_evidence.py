"""Guard fixture evaluations become recorded evidence bundles and a generated results table."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_guard as evaluation
from scripts.evidence import collect, summarize
from tests.support import evidence_fixtures as fx

RECORDED = {"schema": "sentinel-provenance-v1", "source_revision": fx.REVISION, "dirty": False,
            "revision": fx.REVISION, "working_tree_dirty": False}


def guard_run(root, name, *, mutate=None):
    """A real offline evaluation (one repeat) written the way --output writes it."""
    with patch.object(evaluation, "provenance", return_value=dict(RECORDED)):
        result = evaluation.run_evaluation(repeats=1, warmups=0)
    if mutate:
        mutate(result)
    run = Path(root) / name
    fx.write(run / "results.json", result)
    fx.write(run / "README.md", evaluation.markdown_report(result))
    return run


class GuardEvalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"

    def tearDown(self):
        self.temp.cleanup()

    def add(self, run, bundle_id):
        return collect.collect(run, "guard-eval", bundle_id, evidence_root=self.evidence, context=fx.context())

    def test_collected_bundle_is_recorded_and_dated_from_the_run(self):
        run = guard_run(self.root / "source", "g1")
        manifest = self.add(run, "guard-eval-g1")
        self.assertEqual(manifest["provenance"]["kind"], "recorded")
        self.assertEqual(manifest["provenance"]["source_revision"], fx.REVISION)
        self.assertEqual(sorted(item["path"] for item in manifest["files"]), ["README.md", "results.json"])
        started = json.loads((run / "results.json").read_text(encoding="utf-8"))["started_at"]
        self.assertEqual(manifest["captured_at"], started)
        entry = summarize.load_index(self.root)["bundles"][0]
        self.assertEqual((entry["provenance"], entry["path"]), ("recorded", started[:10] + "/guard-eval-g1"))

    def test_table_rows_and_claim_value_come_from_the_newest_bundle(self):
        self.add(guard_run(self.root / "source", "g1"), "guard-eval-g1")
        bundles = summarize.load_bundles(self.root)
        rows = summarize.guard_evaluation(bundles)
        by_configuration = {row[2]: row for row in rows}
        self.assertEqual(set(by_configuration), {"state_policy_only", "full_available", "no_validation"})
        self.assertEqual(by_configuration["full_available"][1], "PRELIMINARY_NOT_FROZEN")
        self.assertEqual(by_configuration["full_available"][3:5], ["measured", "0/27"])
        self.assertEqual(by_configuration["state_policy_only"][4], "21/27")
        self.assertEqual(by_configuration["no_validation"][3:5], ["definitional", "27/27"])
        self.assertEqual(by_configuration["full_available"][8], "0/6")
        self.assertEqual(summarize.outcome(bundles[0]), "PASS")
        self.assertEqual(summarize.guard_unsafe_acceptance(bundles),
                         "state_policy_only 21/27, full_available 0/27, no_validation 27/27 (definitional)")
        markdown = summarize.table(["Bundle", "Status", "Configuration", "Basis", "Unsafe ALLOW", "Unsafe REJECT",
                                    "Unsafe ESCALATE", "Safe ALLOW", "False REJECT", "Errors"], rows)
        self.assertIn("| guard-eval-g1 | PRELIMINARY_NOT_FROZEN | no_validation | definitional | 27/27 |", markdown)

    def test_failed_expectations_count_as_a_failed_run(self):
        self.add(guard_run(self.root / "source", "g1", mutate=lambda r: r.update(expectations_met=False)),
                 "guard-eval-g1")
        self.assertEqual(summarize.outcome(summarize.load_bundles(self.root)[0]), "FAILED")

    def test_no_bundle_gives_an_explicit_value_not_a_number(self):
        self.assertEqual(summarize.guard_unsafe_acceptance([]), "no Guard evaluation")
        self.assertEqual(summarize.guard_evaluation([]), [])


if __name__ == "__main__":
    unittest.main()
