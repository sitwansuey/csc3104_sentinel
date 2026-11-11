"""rcaeval_run.py: resumable runs, the dev-then-freeze gate on eval, outputs and the evidence bundle."""

from contextlib import redirect_stderr
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.datasets import evaluate as ev
from agent.datasets import rcaeval as data
from scripts.evaluation import rcaeval_run as runner
from scripts.evaluation import rcaeval_sample as sampler
from scripts.evidence import collect
from tests.support import evidence_fixtures as fx
from tests.support import rcaeval_fixture as fixture

HAS_PARQUET = bool(importlib.util.find_spec("pyarrow") and importlib.util.find_spec("pandas"))
HAS_RCAEVAL = HAS_PARQUET and bool(importlib.util.find_spec("RCAEval"))
RECORDED = {"schema": "sentinel-provenance-v1", "source_revision": fx.REVISION, "dirty": False,
            "dirty_paths_sha256": None, "dirty_path_count": 0, "python": "3", "host_class": "test-host",
            "tools": {}, "captured_by": "test"}
SMALL = fixture.rows(services=("adservice", "cartservice"), faults={"RE1": ("cpu",), "RE2": ("cpu",)},
                     repetitions={"RE1": 2, "RE2": 2})


class FakeModel:
    """Stands in for Ollama: always names cartservice, the way a real reply would arrive."""

    calls = []

    def ask(self, role, prompt, schema):
        if role == "Planner":
            return {"plan": {"kind": "tool", "goal": "Inspect metrics", "tool": "metrics",
                             "args": {"file": "metric/data.csv"}}}
        if role == "Analyzer":
            return {"summary": "Observed metric change", "complete": True, "confidence": .99, "next_steps": ""}
        if role == "Reporter":
            return {"failures": [{"root cause component": "cartservice"}], "explanation": "Fake model",
                    "limitations": "Synthetic test"}
        raise AssertionError(role)


class InProcess:
    """ev.run's contract without subprocesses, so the fake model can be patched in."""

    def __init__(self, interrupt_after=None):
        self.cases, self.interrupt_after = [], interrupt_after

    def __call__(self, tasks_root, output, method, timeout, *, resume=False, config=None):
        tasks = ev.load_tasks(tasks_root)
        output.mkdir(parents=True, exist_ok=True)
        if not (output / "run.json").exists():
            data.save(output / "run.json", {"method": method, "timeout": timeout, "in_process": True})
        path = output / "predictions.jsonl"
        done = ev.load_predictions(path, tasks) if path.exists() else {}
        overrides = json.loads(Path(config).read_text(encoding="utf-8")) if config else {}
        for case_id in tasks:
            if case_id in done:
                continue
            if self.interrupt_after is not None and len(self.cases) >= self.interrupt_after:
                raise KeyboardInterrupt
            result = output / "cases" / case_id / "result.json"
            with patch("agent.llm.Ollama", return_value=FakeModel()):
                ev.worker(tasks_root / "cases" / case_id, result, method, {"max_rounds": 1, **overrides})
            ev.append_jsonl(path, json.loads(result.read_text(encoding="utf-8")))
            self.cases.append(case_id)
        return path


@unittest.skipUnless(HAS_PARQUET, "optional evaluation dependencies are not installed")
class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir, self.rows, config = fixture.build(self.root / "data", SMALL)
        for target, value in ((data, {"CONFIG": fixture.write_config(self.root / "rcaeval.json", config)}),
                              (runner, {"provenance_block": lambda method: dict(RECORDED),
                                        "model_runtime": lambda config: {"server_version": "test"},
                                        "freeze_committed": lambda path: True})):
            for name, replacement in value.items():
                patcher = patch.object(target, name, replacement)
                patcher.start()
                self.addCleanup(patcher.stop)
        self.sample = self.root / "sample.json"
        sampler.write(sampler.build(self.data_dir), self.sample)
        self.out = self.root / "out"
        self.freeze_file = self.root / "freeze.json"

    def tearDown(self):
        self.temp.cleanup()

    def run_split(self, split, method="sentinel", run_method=None, **options):
        return runner.execute(split=split, method=method, data_dir=self.data_dir, sample_path=self.sample,
                              out_root=self.out, freeze_path=self.freeze_file,
                              run_method=run_method or InProcess(), **options)

    def test_dev_run_with_a_fake_model_writes_scores_versions_and_provenance(self):
        summary = self.run_split("dev")
        dev_cases = json.loads(self.sample.read_text(encoding="utf-8"))["splits"]["dev"]
        expected = sum("_cartservice_" in case for case in dev_cases) / len(dev_cases)
        self.assertEqual((summary["status"], summary["n_cases"], summary["n_errors"]), ("PASS", 2, 0))
        self.assertEqual(summary["service"]["AC@1"], expected)
        self.assertEqual(summary["provenance"]["source_revision"], fx.REVISION)
        output = self.out / "dev/sentinel"
        versions = json.loads((output / "versions.json").read_text(encoding="utf-8"))
        self.assertEqual(versions["dataset"]["revision"], fixture.REVISION)
        self.assertEqual(versions["model"]["runtime"], {"server_version": "test"})
        self.assertEqual(versions["prompt_sha256"], summary["prompt_sha256"])
        self.assertIn("agent/agent.py", versions["files"])
        self.assertEqual(len(ev.read_jsonl(output / "scores/per-case.jsonl")), 2)
        for name in ("summary.json", "versions.json", "scores/summary.json"):
            self.assertNotIn(self.temp.name, (output / name).read_text(encoding="utf-8"))

    def test_eval_is_locked_until_dev_is_complete_and_frozen(self):
        with self.assertRaisesRegex(runner.GateError, "dev split has not been run"):
            self.run_split("eval")
        self.assertFalse((self.out / "eval").exists())
        self.run_split("dev")
        with self.assertRaisesRegex(runner.GateError, "no freeze file"):
            self.run_split("eval")
        runner.freeze(self.out, ["sentinel"], sample_path=self.sample, freeze_path=self.freeze_file)
        summary = self.run_split("eval")
        self.assertEqual((summary["status"], summary["n_cases"]), ("PASS", 4))
        dev = {row["case_id"] for row in ev.read_jsonl(self.out / "dev/sentinel/predictions.jsonl")}
        self.assertFalse(dev & {row["case_id"] for row in ev.read_jsonl(self.out / "eval/sentinel/predictions.jsonl")})

    def test_an_uncommitted_freeze_file_keeps_eval_locked(self):
        self.run_split("dev")
        runner.freeze(self.out, ["sentinel"], sample_path=self.sample, freeze_path=self.freeze_file)
        with patch.object(runner, "freeze_committed", return_value=False), \
                self.assertRaisesRegex(runner.GateError, "not committed"):
            self.run_split("eval")

    def test_a_prompt_change_after_the_freeze_keeps_eval_locked(self):
        first = self.root / "a.json"
        first.write_text(json.dumps({"max_steps": 4}), encoding="utf-8")
        self.run_split("dev", label="sentinel-a", config_path=first)
        runner.freeze(self.out, ["sentinel-a"], sample_path=self.sample, freeze_path=self.freeze_file)
        first.write_text(json.dumps({"max_steps": 5}), encoding="utf-8")
        with self.assertRaises(runner.GateError) as raised:
            self.run_split("eval", label="sentinel-a", config_path=first)
        self.assertIn("changed after its dev run", str(raised.exception))
        self.assertIn("changed after the freeze", str(raised.exception))

    def test_freeze_needs_a_complete_dev_run_and_never_changes_a_frozen_hash(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_split("dev", run_method=InProcess(interrupt_after=1))
        with self.assertRaisesRegex(runner.GateError, "no complete dev run"):
            runner.freeze(self.out, ["sentinel"], sample_path=self.sample, freeze_path=self.freeze_file)
        self.run_split("dev", resume=True)
        runner.freeze(self.out, ["sentinel"], sample_path=self.sample, freeze_path=self.freeze_file)
        frozen = json.loads(self.freeze_file.read_text(encoding="utf-8"))
        frozen["methods"]["sentinel"]["prompt_sha256"] = "0" * 64
        self.freeze_file.write_text(json.dumps(frozen), encoding="utf-8")
        with self.assertRaisesRegex(runner.GateError, "already frozen"):
            runner.freeze(self.out, ["sentinel"], sample_path=self.sample, freeze_path=self.freeze_file)

    def test_interrupted_run_resumes_without_rerunning_completed_cases(self):
        first = InProcess(interrupt_after=1)
        with self.assertRaises(KeyboardInterrupt):
            self.run_split("dev", run_method=first)
        summary = json.loads((self.out / "dev/sentinel/summary.json").read_text(encoding="utf-8"))
        self.assertEqual((summary["status"], summary["complete"], summary["interrupted"]), ("INCOMPLETE", False, True))
        self.assertEqual(summary["n_predicted"], 1)
        with self.assertRaisesRegex(ValueError, "--resume"):
            self.run_split("dev")
        second = InProcess()
        summary2 = self.run_split("dev", run_method=second, resume=True)
        self.assertEqual(summary2["status"], "PASS")
        self.assertEqual(len(first.cases) + len(second.cases), 2)
        self.assertFalse(set(first.cases) & set(second.cases))
        self.assertEqual(summary2["started_at"], summary["started_at"])
        self.assertEqual(len(summary2["invocations"]), 2)

    def test_resume_with_another_configuration_is_refused(self):
        self.run_split("dev")
        other = self.root / "other.json"
        other.write_text(json.dumps({"max_steps": 3}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another method, code or configuration"):
            self.run_split("dev", resume=True, config_path=other)

    def test_a_sample_from_another_index_or_export_is_refused(self):
        path = self.data_dir / "cases.json"
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(ValueError, "not the one the sample was drawn from"):
            self.run_split("dev")
        self.assertFalse((self.out / "dev/export").exists())

    def test_an_export_from_another_sample_is_not_reused(self):
        self.run_split("dev")
        manifest = json.loads(self.sample.read_text(encoding="utf-8"))
        manifest["name"] = "sample-ob-v2"
        self.sample.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "another sample, split or dataset"):
            self.run_split("dev", resume=True)

    def test_command_line_reports_a_locked_eval_split(self):
        with redirect_stderr(io.StringIO()) as errors:
            code = runner.main(["run", "--split", "eval", "--method", "baro", "--data-dir", str(self.data_dir),
                                "--sample", str(self.sample), "--out", str(self.out),
                                "--freeze-file", str(self.freeze_file)])
        self.assertEqual(code, 1)
        self.assertIn("eval split is locked", errors.getvalue())

    def test_run_folder_collects_into_a_recorded_rcaeval_bundle_without_telemetry(self):
        summary = self.run_split("dev")
        output = self.out / "dev/sentinel"
        self.assertTrue(any(path.suffix == ".csv" for path in output.rglob("*")))
        manifest = collect.collect(output, "rcaeval", "rcaeval-dev-sentinel", evidence_root=self.root / "evidence",
                                   context=fx.context())
        self.assertEqual(manifest["provenance"]["kind"], "recorded")
        self.assertEqual(manifest["captured_at"], summary["started_at"])
        self.assertEqual(sorted(item["path"] for item in manifest["files"]),
                         ["predictions.jsonl", "run.json", "scores/per-case.jsonl", "scores/summary.json",
                          "summary.json", "versions.json"])

    @unittest.skipUnless(HAS_RCAEVAL, "the pinned RCAEval package is not installed")
    def test_baro_runs_through_the_real_worker_and_ranks_the_spike_first(self):
        summary = runner.execute(split="dev", method="baro", data_dir=self.data_dir, sample_path=self.sample,
                                 out_root=self.out, freeze_path=self.freeze_file, timeout=120)
        self.assertEqual((summary["status"], summary["n_errors"]), ("PASS", 0))
        self.assertEqual(summary["service"]["AC@1"], 1.0)
        self.assertIsNone(json.loads((self.out / "dev/baro/versions.json").read_text(encoding="utf-8"))["model"])


class FingerprintTests(unittest.TestCase):
    def test_prompt_hash_tracks_files_and_configuration(self):
        first = runner.fingerprint("baro", {})
        self.assertEqual(first, runner.fingerprint("baro", {}))
        self.assertNotEqual(first["prompt_sha256"], runner.fingerprint("baro", {"x": 1})["prompt_sha256"])
        self.assertIn("agent/datasets/baselines.py", first["files"])
        self.assertIn("agent/datasets/casetoolkit.py", runner.fingerprint("casetoolkit", {})["files"])
        with patch.object(runner, "method_files", return_value={"agent/agent.py": "changed"}):
            self.assertNotEqual(first["prompt_sha256"], runner.fingerprint("baro", {})["prompt_sha256"])
        with self.assertRaises(ValueError):
            runner.method_files("unknown")

    def test_llm_configuration_matches_the_harness_and_forces_abstention(self):
        config = runner.effective_config("sentinel", None)
        self.assertTrue(config["allow_abstention"])
        self.assertEqual(config["model"], json.loads(runner.RCA_CONFIG.read_text(encoding="utf-8"))["model"])
        self.assertEqual(runner.effective_config("baro", None), {})

    def test_freeze_outside_the_repository_is_not_committed(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertFalse(runner.freeze_committed(Path(temp) / "freeze.json"))


if __name__ == "__main__":
    unittest.main()
