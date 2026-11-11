"""Per-stage latency ledger: exact deltas, honest gaps, no model text, and a CI-checked table."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.analysis import timeline
from scripts import latency_report
from scripts.evidence import collect, summarize
from tests.support import evidence_fixtures as fx

FORBIDDEN_KEYS = {"prompt", "prompts", "message", "messages", "text", "rationale", "response", "detail"}


def full_run(root, name="run", *, injected="2026-11-11T10:00:00Z", outcome=True, progress=True, load=0.01,
             failure=None):
    """A synthetic agent run whose stage boundaries are whole seconds apart."""
    run = Path(root) / name
    summary = {"status": "PASS" if outcome and not failure else "FAILED", "started_at": "2026-11-11T09:59:00Z",
               "model": "qwen3:14b", "checks": []}
    if injected:
        summary["fault_injected_at"] = injected
    if failure:
        summary["agent_failure"] = {"error": "RuntimeError: secret prompt text", "seconds": 12.5}
    fx.write(run / "summary.json", summary)
    fx.write(run / "primary-incident.json", {
        "observed_at": "2026-11-11T10:00:10Z",
        "signals": [{"at": "2026-11-11T09:59:58Z", "source": "event", "detail": "older event"},
                    {"at": "2026-11-11T10:00:03Z", "source": "deployment_status", "detail": "unavailable"},
                    {"at": "2026-11-11T10:00:05Z", "source": "pod_status", "detail": "crash"}]})
    folder = run / "approved-rollback"
    fx.write(folder / "model-calls.json", [
        {"role": "Analyzer", "status": "completed", "seconds": 20.0, "load_duration_seconds": load,
         "prompt_eval_duration_seconds": 2.0, "eval_duration_seconds": 17.0, "prompt_tokens": 2000,
         "output_tokens": 200, "prompt": "never copy this", "message": "nor this"},
        {"role": "Recovery Planner", "status": "completed", "seconds": 10.0, "load_duration_seconds": 0.002,
         "prompt_tokens": 1500, "output_tokens": 100}])
    if progress:
        lines = [{"at": "2026-11-11T10:00:11+00:00", "stage": "investigation", "status": "started"},
                 {"at": "2026-11-11T10:00:40+00:00", "stage": "model", "status": "completed", "text": "no"}]
        fx.write(folder / "planning-progress.jsonl", "".join(json.dumps(line) + "\n" for line in lines))
    if failure:
        return run
    fx.write(folder / "proposal.json", {"action": "ROLLBACK", "rationale": "never copy this either"})
    fx.write(folder / "initial-guard.json", {"verdict": "ALLOW", "checked_at": "2026-11-11T10:00:41Z"})
    fx.write(folder / "execution.json", {"status": "APPLIED", "recorded_at": "2026-11-11T10:00:42Z",
                                         "details": "not copied"})
    if outcome:
        fx.write(folder / "outcome.json", {"status": "RECOVERED", "window_end": "2026-11-11T10:01:30Z"})
    return run


def keys(value):
    if isinstance(value, dict):
        return set(value) | {key for item in value.values() for key in keys(item)}
    if isinstance(value, list):
        return {key for item in value for key in keys(item)}
    return set()


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def row(self, run, kind=timeline.RUN_FOLDER, **options):
        return timeline.run_timeline([(run, kind)], run=run.name, **options)

    def test_synthetic_run_gives_exact_stage_deltas(self):
        row = self.row(full_run(self.root))
        self.assertEqual(row["start_basis"], "fault_injected_at")
        self.assertEqual(row["stages"], {"detection": 10.0, "planning": 30.0, "model": 30.0, "guard": 1.0,
                                         "decision": 31.0, "execution": 1.0, "verification": 48.0,
                                         "repair": 42.0, "total": 90.0})
        self.assertEqual((row["planning_mode"], row["status"], row["outcome"], row["failure_stage"]),
                         ("agent", "PASS", "RECOVERED", None))
        self.assertEqual([call["cold"] for call in row["calls"]], [False, False])
        self.assertEqual(row["calls"][0]["prompt_tokens"], 2000)

    def test_missing_files_give_null_not_zero(self):
        row = self.row(full_run(self.root, outcome=False))
        self.assertIsNone(row["stages"]["verification"])
        self.assertIsNone(row["stages"]["total"])
        self.assertEqual(row["stages"]["execution"], 1.0)
        self.assertEqual(row["failure_stage"], "verification")
        empty = self.root / "empty"
        empty.mkdir()
        bare = self.row(empty)
        self.assertTrue(all(value is None for value in bare["stages"].values()))
        self.assertIn("summary.json missing", bare["schema_notes"])
        summary = timeline.describe([None, None])
        self.assertEqual(summary, {"n": 0, "median": None, "p90": None, "p95": None, "min": None, "max": None})

    def test_without_injection_time_detection_starts_at_the_first_failing_snapshot(self):
        row = self.row(full_run(self.root, injected=None))
        # The 09:59:58 event is ignored; the first failing snapshot is 10:00:03.
        self.assertEqual(row["start_basis"], "from_first_failing_snapshot")
        self.assertEqual(row["stages"]["detection"], 7.0)
        self.assertEqual(row["stages"]["total"], 87.0)
        self.assertTrue(any("first failing snapshot" in note for note in row["schema_notes"]))
        self.assertNotIn("fault_injected_at", json.dumps(row))

    def test_timeline_phase_is_used_when_the_summary_field_is_absent(self):
        run = full_run(self.root, injected=None)
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        summary["timeline"] = [{"phase": "fault_injected", "at": "2026-11-11T10:00:01Z", "scenario": "primary"}]
        fx.write(run / "summary.json", summary)
        row = self.row(run)
        self.assertEqual((row["start_basis"], row["stages"]["detection"]), ("timeline", 9.0))

    def test_failed_agent_run_is_kept_with_its_failure_stage(self):
        row = self.row(full_run(self.root, failure=True))
        self.assertEqual((row["status"], row["failure_stage"]), ("FAILED", "planning"))
        self.assertEqual(row["planning_seconds_before_failure"], 12.5)
        self.assertIsNone(row["stages"]["model"])
        self.assertIsNone(row["stages"]["planning"])
        self.assertEqual(row["stages"]["detection"], 10.0)
        self.assertEqual(len(row["calls"]), 2)
        summary = timeline.aggregate([row, self.row(full_run(self.root, "ok"))])["agent"]
        self.assertEqual((summary["runs"], summary["passed"], summary["failure_stages"]), (2, 1, {"planning": 1}))
        self.assertEqual(summary["stages"]["model"]["n"], 1)

    def test_older_agent_run_without_a_proposal_stopped_in_planning(self):
        run = full_run(self.root, failure=True)
        summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
        del summary["agent_failure"]
        fx.write(run / "summary.json", summary)
        self.assertEqual(self.row(run)["failure_stage"], "planning")
        self.assertEqual(self.row(run, planning_mode="harness")["failure_stage"], "decision")

    def test_cold_calls_are_classified_and_bucketed(self):
        cold = self.row(full_run(self.root, "cold", load=25.6))
        warm = self.row(full_run(self.root, "warm"))
        unknown = self.row(full_run(self.root, "unknown", load=None))
        self.assertEqual([call["cold"] for call in cold["calls"]], [True, False])
        self.assertIsNone(unknown["calls"][0]["cold"])
        self.assertEqual(timeline.temperature(cold), "cold")
        self.assertEqual(timeline.temperature(warm), "warm")
        self.assertEqual(timeline.temperature(unknown), "unclassified")
        summary = timeline.aggregate([cold, warm, unknown])["agent"]
        self.assertEqual({name: values["n"] for name, values in summary["model_by_temperature"].items()},
                         {"warm": 1, "cold": 1, "unclassified": 1})
        self.assertEqual(summary["calls_by_temperature"]["cold"]["n"], 1)
        self.assertEqual(summary["calls_by_temperature"]["unclassified"]["n"], 1)
        self.assertEqual(summary["cold_load"], {"n": 1, "median": 25.6, "p90": 25.6, "p95": 25.6,
                                                "min": 25.6, "max": 25.6})

    def test_percentiles_use_nearest_rank(self):
        values = timeline.describe(list(range(1, 21)))
        self.assertEqual((values["n"], values["median"], values["p90"], values["p95"]), (20, 10.5, 18, 19))
        self.assertEqual(timeline.describe([4.2])["p95"], 4.2)

    def test_output_never_carries_prompt_message_or_text_keys(self):
        rows = [self.row(full_run(self.root, "a")), self.row(full_run(self.root, "b", failure=True))]
        output = json.dumps({"rows": rows, "aggregate": timeline.aggregate(rows)})
        self.assertFalse(keys(json.loads(output)) & FORBIDDEN_KEYS)
        for secret in ("never copy this", "nor this", "secret prompt text", "not copied", "older event"):
            self.assertNotIn(secret, output)

    def test_bundle_first_with_run_folder_supplement_marks_each_stage_source(self):
        run = full_run(self.root)
        bundle = self.root / "bundle"
        for relative in ("summary.json", "primary-incident.json", "approved-rollback/model-calls.json",
                         "approved-rollback/proposal.json", "approved-rollback/initial-guard.json",
                         "approved-rollback/execution.json", "approved-rollback/outcome.json"):
            fx.write(bundle / relative, (run / relative).read_bytes())
        both = timeline.run_timeline([(bundle, timeline.BUNDLE), (run, timeline.RUN_FOLDER)])
        alone = timeline.run_timeline([(bundle, timeline.BUNDLE)])
        self.assertEqual(both["stage_sources"]["planning"], timeline.RUN_FOLDER)
        self.assertEqual(both["stage_sources"]["guard"], timeline.RUN_FOLDER)
        self.assertEqual(both["stage_sources"]["decision"], timeline.BUNDLE)
        self.assertIsNone(alone["stages"]["planning"])
        self.assertIsNone(alone["stages"]["guard"])
        for name, source in both["stage_sources"].items():
            if source == timeline.BUNDLE:
                self.assertEqual(both["stages"][name], alone["stages"][name])


def evidence_tree(root):
    """Two collected agent bundles and one harness bundle; the run folders stay as the supplement."""
    root, runs = Path(root), Path(root) / "runs"
    evidence = root / "evidence"
    inferred = {"inferred_revision": fx.REVISION, "inferred_basis": "fixture", "context": fx.context()}
    collect.collect(full_run(runs, "boutique-live-20261111T100000Z-aaaaaa"), "agent", "agent-one",
                    evidence_root=evidence, **inferred)
    collect.collect(full_run(runs, "boutique-live-20261111T110000Z-bbbbbb", failure=True), "agent", "agent-two",
                    evidence_root=evidence, date="2026-11-11", **inferred)
    collect.collect(fx.connected_run(runs, "boutique-live-20261111T090000Z-cccccc"), "connected-harness",
                    "harness-one", evidence_root=evidence, **inferred)
    for relative, names in summarize.TARGETS.items():
        fx.write(root / relative, "".join(f"<!-- BEGIN GENERATED:{name} -->\n<!-- END GENERATED:{name} -->\n"
                                          for name in names))
    return root, runs


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root, self.runs = evidence_tree(self.temp.name)
        self.out = self.root / summarize.LATENCY_DIR / "2026-11-12"

    def tearDown(self):
        self.temp.cleanup()

    def write(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return latency_report.main(["--root", str(self.root), "--runs-root", str(self.runs),
                                        "--out", str(self.out)])

    def ledger(self):
        return json.loads((self.out / "timings.json").read_text(encoding="utf-8"))

    def test_ledger_has_one_row_per_bundle_and_reads_run_folders_only_as_a_supplement(self):
        self.assertEqual(self.write(), 0)
        rows = {row["bundle_id"]: row for row in self.ledger()["rows"]}
        self.assertEqual(set(rows), {"agent-one", "agent-two", "harness-one"})
        self.assertEqual(rows["agent-one"]["stages"]["guard"], 1.0)
        self.assertEqual(rows["agent-one"]["stage_sources"]["guard"], timeline.RUN_FOLDER)
        self.assertEqual(rows["harness-one"]["planning_mode"], "harness")
        self.assertTrue(all(row["run_folder_read"] for row in rows.values()))
        self.assertFalse(keys(self.ledger()["rows"]) & FORBIDDEN_KEYS)

    def test_existing_ledger_is_not_overwritten(self):
        self.assertEqual(self.write(), 0)
        before = (self.out / "timings.json").read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.write(), 1)
        self.assertEqual((self.out / "timings.json").read_bytes(), before)

    def test_without_out_nothing_is_written(self):
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(latency_report.main(["--root", str(self.root)]), 0)
        self.assertFalse((self.root / summarize.LATENCY_DIR).exists())
        self.assertIn("agent: 2 runs, 1 PASS", printed.getvalue())

    def test_summarize_renders_the_ledger_and_check_passes(self):
        self.write()
        out = io.StringIO()
        self.assertEqual(summarize.run(self.root, out=out), 0, out.getvalue())
        self.assertEqual(summarize.run(self.root, check=True, out=io.StringIO()), 0)
        text = (self.root / "docs/results.md").read_text(encoding="utf-8")
        self.assertIn("- agent: 2 runs, 1 PASS; stopped at: planning x1.", text)
        self.assertIn("| agent | Guard: planning end -> ALLOW (incl. lab approval) | 1 | 1.0 | 1.0 | 1.0 | 1.0 | "
                      "run folder |", text)
        self.assertIn("| agent | detection: start -> incident observed | 2 | 10.0 | 10.0 | 10.0 | 10.0 | "
                      "bundle; start: injection x2 |", text)
        self.assertIn("| harness | planning: incident -> last planning record | 0 | n/a | n/a | n/a | n/a | - |",
                      text)
        self.assertIn("| agent | model load time of cold calls | 0 | n/a | n/a | n/a |", text)

    def test_no_ledger_renders_a_placeholder_and_is_not_an_error(self):
        bundles = summarize.load_bundles(self.root)
        self.assertEqual(summarize.latency_tables(None, bundles), (summarize.NO_LEDGER, summarize.NO_LEDGER))
        self.assertEqual(summarize.check_latency(self.root), [])

    def test_check_fails_when_a_bundle_value_is_edited(self):
        self.write()
        ledger = self.ledger()
        row = next(row for row in ledger["rows"] if row["bundle_id"] == "agent-one")
        row["stages"]["execution"] = 0.1
        (self.out / "timings.json").write_text(json.dumps(ledger), encoding="utf-8")
        problems = summarize.check_latency(self.root)
        self.assertEqual(len(problems), 1)
        self.assertIn("agent-one execution 0.1 (bundle) differs from the bundle (1.0)", problems[0])
        harness = next(row for row in ledger["rows"] if row["bundle_id"] == "harness-one")
        harness["stages"]["execution"] = 0.5
        (self.out / "timings.json").write_text(json.dumps(ledger), encoding="utf-8")
        self.assertIn("harness-one execution 0.5 (None) differs from the bundle (None)",
                      "\n".join(summarize.check_latency(self.root)))

    def test_check_fails_when_a_bundle_is_missing_or_a_row_is_unknown(self):
        self.write()
        ledger = self.ledger()
        removed = ledger["rows"].pop()
        ledger["rows"].append(dict(removed, bundle_id="never-collected"))
        (self.out / "timings.json").write_text(json.dumps(ledger), encoding="utf-8")
        problems = summarize.check_latency(self.root)
        self.assertTrue(any("bundle " + removed["bundle_id"] + " is missing" in line for line in problems))
        self.assertTrue(any("row never-collected is not a bundle" in line for line in problems))
        self.assertEqual(summarize.run(self.root, out=io.StringIO()), 1)
        self.assertEqual(summarize.run(self.root, check=True, out=io.StringIO()), 1)

    def test_run_folder_values_are_not_rechecked_against_the_bundle(self):
        self.write()
        ledger = self.ledger()
        row = next(row for row in ledger["rows"] if row["bundle_id"] == "agent-one")
        row["stages"]["guard"] = 2.0
        (self.out / "timings.json").write_text(json.dumps(ledger), encoding="utf-8")
        self.assertEqual(summarize.check_latency(self.root), [])

    def test_withdrawn_bundles_drop_out_of_the_table(self):
        self.write()
        index_path = self.root / "evidence/index.json"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        for entry in index["bundles"]:
            if entry["bundle_id"] == "agent-two":
                entry.update(status="withdrawn", reason="fixture")
        index_path.write_text(json.dumps(index), encoding="utf-8")
        rows = summarize.latency_rows(self.ledger(), summarize.load_bundles(self.root))
        self.assertEqual({row["bundle_id"] for row in rows}, {"agent-one", "harness-one"})
        self.assertEqual(summarize.check_latency(self.root), [])


class CommittedLedgerTests(unittest.TestCase):
    def test_committed_ledger_matches_the_committed_bundles(self):
        root = Path(__file__).resolve().parents[1]
        relative, ledger = summarize.load_latency(root)
        self.assertIsNotNone(ledger, "no committed latency ledger")
        self.assertEqual(ledger["schema"], latency_report.LEDGER_SCHEMA)
        self.assertEqual(summarize.check_latency(root), [])
        self.assertFalse(keys(ledger["rows"]) & FORBIDDEN_KEYS)


class HarnessInjectionTimeTests(unittest.TestCase):
    def test_inject_fault_records_the_first_injection_time(self):
        from scripts.test_boutique_live import BoutiqueExperiment
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            try:
                self.assertNotIn("fault_injected_at", experiment.summary)
                with patch.object(experiment, "kubectl") as kubectl, \
                        patch("experiments.boutique.harness.live.utc",
                              side_effect=["2026-11-11T10:00:00Z", "2026-11-11T10:05:00Z"]):
                    experiment.inject_fault()
                    experiment.inject_fault()
                self.assertEqual(kubectl.call_count, 2)
                self.assertEqual(experiment.summary["fault_injected_at"], "2026-11-11T10:00:00Z")
            finally:
                experiment.store.close()

    def test_failed_injection_records_no_time(self):
        from scripts.test_boutique_live import BoutiqueExperiment
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            try:
                with patch.object(experiment, "kubectl", side_effect=RuntimeError("kubectl operation failed")):
                    with self.assertRaises(RuntimeError):
                        experiment.inject_fault()
                self.assertNotIn("fault_injected_at", experiment.summary)
            finally:
                experiment.store.close()


if __name__ == "__main__":
    unittest.main()
