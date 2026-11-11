"""rcaeval_report.py: Wilson intervals, exact McNemar, bootstrap, Holm and golden report output.

The golden files in tests/fixtures/rcaeval/ were produced by build() and
markdown() from the synthetic folders below and checked against the hand
computations in the other tests. Regenerate them only for an intended change to
the report, and review the difference.
"""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evaluation import rcaeval_report as report

GOLDEN = Path(__file__).resolve().parent / "fixtures/rcaeval"
ANSWERS = "a" * 64
# Service and indicator ranks per case (None is a miss), status and wall time, for ten cases.
METHODS = {
    "baro": ("baro", [1, 1, 2, None, 1, 3, 1, None, 2, 1], [1, 2, 3, None, 1, None, 2, None, 4, 1],
             ["ok"] * 10, [0.4, 0.5, 0.6, 0.5, 0.4, 0.7, 0.5, 0.6, 0.5, 0.4]),
    "anomaly-rank": ("anomaly-rank", [1, 2, None, None, 1, 4, 2, None, 1, 1], [2, 3, None, None, 1, None, 5, None, 1, 1],
                     ["ok"] * 10, [0.3, 0.4, 0.4, 0.5, 0.3, 0.6, 0.4, 0.5, 0.4, 0.3]),
    "sentinel": ("sentinel", [1, None, None, None, 1, None, 1, None, None, 1], [None] * 10,
                 ["ok", "ok", "abstained", "ok", "error", "ok", "ok", "ok", "ok", "ok"],
                 [95.0, 120.0, 80.0, 110.0, 600.0, 130.0, 90.0, 100.0, 140.0, 105.0]),
}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def result_folder(root, label, *, split="eval", answers=ANSWERS, status="PASS", dirty=False, prompt="p" * 64,
                  invocations=None, rows=None):
    method, service, indicator, statuses, walls = METHODS[label]
    folder = Path(root) / split / label
    ids = [f"case-{index:016x}" for index in range(len(service))]
    per_case = rows if rows is not None else [
        {"case_id": case_id, "service_rank": s, "indicator_rank": i, "status": state}
        for case_id, s, i, state in zip(ids, service, indicator, statuses, strict=True)]
    provenance = {"source_revision": "1" * 40, "dirty": dirty}
    write_json(folder / "summary.json", {
        "schema": report.RUN_SCHEMA, "status": status, "split": split, "method": method, "label": label,
        "sample": "sample-ob-v1", "sample_sha256": "s" * 64, "answers_sha256": answers, "prompt_sha256": prompt,
        "n_cases": len(per_case), "n_predicted": len(per_case), "n_errors": statuses.count("error"),
        "provenance": provenance, "invocations": invocations or [{"at": "2026-11-12T00:00:00Z", **provenance}]})
    write_json(folder / "scores/summary.json", {"answers_sha256": answers})
    (folder / "scores/per-case.jsonl").write_text("".join(json.dumps(r) + "\n" for r in per_case), encoding="utf-8")
    tokens = {"input_tokens": 1000, "output_tokens": 100} if method == "sentinel" else {}
    (folder / "predictions.jsonl").write_text("".join(
        json.dumps({"case_id": case_id, "ranks": [], "status": state, "usage": {"wall_time_s": wall, **tokens}}) + "\n"
        for case_id, state, wall in zip(ids, statuses, walls, strict=True)), encoding="utf-8")
    return folder


class StatisticsTests(unittest.TestCase):
    def test_wilson_edges_and_a_known_value(self):
        self.assertIsNone(report.wilson(0, 0))
        self.assertEqual(report.wilson(0, 7)[0], 0.0)
        self.assertEqual(report.wilson(7, 7)[1], 1.0)
        low, high = report.wilson(5, 10)
        self.assertAlmostEqual(low, 0.2366, places=4)
        self.assertAlmostEqual(high, 0.7634, places=4)
        low, high = report.wilson(0, 10)
        self.assertAlmostEqual(high, 0.2775, places=4)
        with self.assertRaises(ValueError):
            report.wilson(11, 10)

    def test_exact_mcnemar(self):
        self.assertEqual(report.mcnemar_exact(0, 0), 1.0)
        self.assertEqual(report.mcnemar_exact(3, 3), 1.0)
        self.assertAlmostEqual(report.mcnemar_exact(0, 5), 0.0625)
        self.assertAlmostEqual(report.mcnemar_exact(6, 1), 0.125)

    def test_holm_adjustment(self):
        adjusted = report.holm([0.01, 0.04, 0.03])
        for value, expected in zip(adjusted, [0.03, 0.06, 0.06], strict=True):
            self.assertAlmostEqual(value, expected)
        self.assertEqual(report.holm([]), [])
        self.assertEqual(report.holm([0.6, 0.9]), [1.0, 1.0])

    def test_bootstrap_is_seeded_and_degenerate_samples_collapse(self):
        values = [0, 1, 1, 0, 1, 0.2, 0.8]
        first = report.bootstrap_ci(values, seed=3, resamples=400)
        self.assertEqual(first, report.bootstrap_ci(values, seed=3, resamples=400))
        self.assertNotEqual(first, report.bootstrap_ci(values, seed=4, resamples=400))
        self.assertLess(first[0], sum(values) / len(values))
        self.assertGreater(first[1], sum(values) / len(values))
        self.assertEqual(report.bootstrap_ci([0.5] * 6, seed=1, resamples=50), [0.5, 0.5])
        self.assertIsNone(report.bootstrap_ci([], seed=1, resamples=50))
        with self.assertRaises(ValueError):
            report.bootstrap_ci([1], seed=1, resamples=0)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def folders(self, **options):
        return [result_folder(self.root, label, **options) for label in METHODS]

    def test_report_matches_the_golden_files(self):
        built = report.build(self.folders(), seed=11, resamples=1000)
        golden = json.loads((GOLDEN / "report-golden.json").read_text(encoding="utf-8"))
        self.assertEqual(built, golden)
        self.assertEqual(report.markdown(built),
                         (GOLDEN / "report-golden.md").read_text(encoding="utf-8").replace("\r\n", "\n"))

    def test_metrics_agree_with_hand_counts(self):
        built = report.build(self.folders(), seed=11, resamples=200)
        baro, sentinel = built["methods"]["baro"], built["methods"]["sentinel"]
        self.assertEqual((baro["AC@1"]["count"], baro["AC@3"]["count"], baro["AC@5"]["count"]), (5, 8, 8))
        self.assertAlmostEqual(baro["Avg@5"]["value"], (5 + 7 + 8 + 8 + 8) / 50)
        self.assertEqual(baro["AC@1"]["ci95"], [round(v, 6) for v in report.wilson(5, 10)])
        self.assertEqual((sentinel["abstention_rate"]["count"], sentinel["error_rate"]["count"]), (1, 1))
        self.assertEqual(sentinel["AC@3"]["value"], sentinel["AC@1"]["value"])
        self.assertTrue(sentinel["top1_only"])
        self.assertIsNone(sentinel["unsafe_action_rate"])
        self.assertEqual(sentinel["latency_s"]["max"], 600.0)
        self.assertEqual(sentinel["model_tokens"], {"input_tokens": 10000, "output_tokens": 1000})
        self.assertIsNone(baro["model_tokens"])
        pair = next(p for p in built["paired"] if (p["a"], p["b"]) == ("baro", "sentinel"))
        self.assertEqual((pair["only_a_correct"], pair["only_b_correct"], pair["mcnemar_p"]), (1, 0, 1.0))
        self.assertEqual(built["deviations"], [])

    def test_mixed_answers_splits_or_labels_are_refused(self):
        baro = result_folder(self.root / "a", "baro")
        with self.assertRaisesRegex(ValueError, "answers_sha256"):
            report.build([baro, result_folder(self.root / "b", "sentinel", answers="b" * 64)])
        with self.assertRaisesRegex(ValueError, "mix splits"):
            report.build([baro, result_folder(self.root / "c", "sentinel", split="dev")])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            report.build([baro, result_folder(self.root / "d", "baro")])
        with self.assertRaisesRegex(ValueError, "unknown labels"):
            report.build([baro], pairs=[("baro", "sentinel")])
        with self.assertRaisesRegex(ValueError, "same cases"):
            report.build([baro, result_folder(self.root / "e", "sentinel", rows=[
                {"case_id": f"case-{i:016x}", "service_rank": 1, "indicator_rank": None, "status": "ok"}
                for i in range(1, 11)])])

    def test_deviations_are_listed_from_the_result_files(self):
        folders = [result_folder(self.root, "baro", status="INCOMPLETE"),
                   result_folder(self.root, "anomaly-rank", dirty=True),
                   result_folder(self.root, "sentinel", invocations=[
                       {"at": "x", "source_revision": "1" * 40, "dirty": False},
                       {"at": "y", "source_revision": "2" * 40, "dirty": False}])]
        freeze = {"methods": {"baro": {"prompt_sha256": "p" * 64}, "sentinel": {"prompt_sha256": "q" * 64}}}
        found = report.build(folders, resamples=50, freeze=freeze)["deviations"]
        self.assertTrue(any(item.startswith("baro: run status INCOMPLETE") for item in found))
        self.assertIn("anomaly-rank: run from a dirty or unknown working tree", found)
        self.assertIn("sentinel: resumed at a different source revision", found)
        self.assertIn("anomaly-rank: not in the freeze file", found)
        self.assertIn("sentinel: prompt hash differs from the freeze file", found)
        self.assertTrue(any(item.startswith("methods were run at different source revisions") for item in found))

    def test_command_line_writes_both_files(self):
        out = self.root / "report"
        with redirect_stdout(io.StringIO()):
            code = report.main(["--results", *map(str, self.folders()), "--out", str(out), "--resamples", "100",
                                "--pairs", "sentinel:baro"])
        self.assertEqual(code, 0)
        written = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertEqual([(p["a"], p["b"]) for p in written["paired"]], [("sentinel", "baro")])
        self.assertIn("| sentinel | baro | 10 |", (out / "report.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
