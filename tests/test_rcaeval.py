"""Strict non-code selection, blinded evaluation, scoring and failed-run accounting."""

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from agent.datasets import evaluate as ev
from agent.datasets import rcaeval as data


def case(suite="RE1", system="ob", fault="cpu"):
    return {"case": f"{suite.lower()}{system}_cartservice_{fault}_1", "suite": suite,
            "system": system, "system_name": data.settings()["systems"][system],
            "dataset": f"{suite}-{system.upper()}", "fault": fault, "root_cause_service": "cartservice",
            "repetition": 1, "inject_time": 1000, "has_logs": False, "has_traces": False,
            "has_root_cause_file": False}


def answer(identifier="a", service="cartservice", fault="cpu", dataset="RE1-OB"):
    return {"case_id": identifier, "dataset": dataset, "system": "ob", "service": service,
            "fault": fault, "indicator": ev.indicator(fault, dataset.split("-")[0]),
            "n_candidate_services": 10, "n_candidate_indicators": 20,
            "answer_in_candidates": True, "indicator_in_candidates": True}


class SelectionTests(unittest.TestCase):
    def test_sentinel_timing_converts_epoch_without_model_arithmetic(self):
        self.assertEqual(ev.sentinel_timing({"inject_time": 1705351632,
                         "standard_window": {"start": 1705351032, "end": 1705352232}}),
                         {"anomaly": "2024-01-15T20:47:12+00:00",
                          "start": "2024-01-15T20:37:12+00:00", "end": "2024-01-15T20:57:12+00:00"})
        for start, end in [(2000, 1000), (1001, 1600), (float('nan'), 1600), ('400', 1600)]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                ev.sentinel_timing({"inject_time": 1000, "standard_window": {"start": start, "end": end}})

    def test_all_three_systems_and_only_non_code_suites(self):
        config = data.settings()
        rows = [case(suite, system) for suite in ["RE1", "RE2"] for system in ["ob", "ss", "tt"]]
        config["expected_counts"] = {r["dataset"]: 1 for r in rows}
        selected = data.select_cases(rows + [case("RE3", "ob", "f1")], config)
        self.assertEqual(len(selected), 6)
        self.assertEqual({r["system"] for r in selected}, {"ob", "ss", "tt"})
        self.assertNotIn("RE3", {r["suite"] for r in selected})

    def test_non_code_whitelist_rejects_mislabeled_code_error(self):
        with self.assertRaises(ValueError):
            data.select_cases([case(fault="f1")], data.settings())

    def test_metadata_and_path_must_agree(self):
        row = case()
        row["case"] = "re3ob_cartservice_f1_1"
        with self.assertRaises(ValueError):
            data.select_cases([row], data.settings())

    def test_pinned_counts_and_duplicates_are_enforced(self):
        with self.assertRaises(ValueError):
            data.select_cases([case()], data.settings())
        config = data.settings()
        config["expected_counts"] = {"RE1-OB": 2}
        with self.assertRaises(ValueError):
            data.select_cases([case(), case()], config)

    def test_file_plan_never_includes_excluded_case(self):
        row = case()
        siblings = [{"rfilename": row["case"] + "/" + name, "size": 10, "blobId": "a" * 40}
                    for name in ["metrics.parquet", "inject_time.txt"]]
        siblings.append({"rfilename": "re3ob_cartservice_f1_1/metrics.parquet", "size": 10, "blobId": "a" * 40})
        plan = data.file_plan([row], siblings)
        self.assertEqual(len(plan), 2)
        self.assertTrue(all(item["path"].startswith("re1ob_") for item in plan))

    def test_missing_modality_and_unexpected_paths_rejected(self):
        row = case()
        row["has_logs"] = True
        siblings = [{"rfilename": row["case"] + "/" + name, "size": 10, "blobId": "a" * 40}
                    for name in ["metrics.parquet", "inject_time.txt"]]
        with self.assertRaises(ValueError):
            data.file_plan([row], siblings)
        siblings.append({"rfilename": row["case"] + "/../../escape", "size": 10, "blobId": "a" * 40})
        with self.assertRaises(ValueError):
            data.file_plan([row], siblings)

    def test_source_checksums_detect_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "data"
            path.write_bytes(b"abc")
            spec = {"bytes": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
            self.assertTrue(data.verify(path, spec))
            path.write_bytes(b"abd")
            self.assertFalse(data.verify(path, spec))


class ScoringTests(unittest.TestCase):
    def test_upstream_fault_indicator_mapping(self):
        self.assertEqual(ev.indicator("disk", "RE1"), "latency")
        self.assertEqual(ev.indicator("disk", "RE2"), "diskio")
        for fault in ["delay", "loss"]:
            self.assertEqual(ev.indicator(fault, "RE2"), "latency")
        with self.assertRaises(ValueError):
            ev.indicator("f1", "RE3")

    def test_missing_predictions_remain_in_denominator(self):
        summary, rows = ev.score([answer("a"), answer("b")], {"a": {"ranks": ["cartservice_cpu"]}})
        self.assertEqual(summary["service"]["AC@1"], .5)
        self.assertEqual(summary["indicator"]["Avg@5"], .5)
        self.assertEqual(summary["n_cases"], 2)
        self.assertIsNone(rows[1]["service_rank"])

    def test_service_deduplication_differs_from_indicator_ranking(self):
        ranks = ["frontend_cpu", "frontend_mem", "cartservice_cpu"]
        summary, rows = ev.score([answer()], {"a": {"ranks": ranks}})
        self.assertEqual(rows[0]["service_rank"], 2)
        self.assertEqual(rows[0]["indicator_rank"], 3)
        self.assertAlmostEqual(summary["service"]["Avg@5"], .8)
        self.assertAlmostEqual(summary["indicator"]["Avg@5"], .6)

    def test_database_normalization_and_service_only_rank(self):
        summary, rows = ev.score([answer(service="carts")], {"a": {"ranks": ["carts-db"]}})
        self.assertEqual(rows[0]["service_rank"], 1)
        self.assertIsNone(rows[0]["indicator_rank"])
        self.assertEqual(summary["indicator"]["AC@5"], 0)

    def test_errors_abstentions_and_empty_rankings_score_as_misses(self):
        predictions = {"a": {"ranks": [], "status": "error"}, "b": {"ranks": [], "status": "abstained"}}
        summary, _ = ev.score([answer("a"), answer("b")], predictions)
        self.assertEqual(summary["service"]["AC@5"], 0)
        self.assertEqual(summary["n_errors"], 1)
        self.assertEqual(summary["n_nonempty"], 0)

    def test_unknown_duplicate_and_malformed_predictions_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "predictions.jsonl"
            for rows in [[{"case_id": "unknown", "ranks": []}], [{"case_id": "a", "ranks": "cpu"}],
                         [{"case_id": "a", "ranks": ["cartservice"], "status": "error"}],
                         [{"case_id": "a", "ranks": []}, {"case_id": "a", "ranks": []}]]:
                path.write_text("\n".join(json.dumps(r) for r in rows))
                with self.assertRaises(ValueError):
                    ev.load_predictions(path, {"a"})

    def test_candidate_header_matches_reference_rules(self):
        self.assertEqual(ev.metric_columns(["time", "time.1", "a_latency-50", "a_latency-90", "a_cpu"]),
                         ["a_latency", "a_cpu"])

    def test_chance_and_lift_use_full_candidate_pool(self):
        summary, _ = ev.score([answer()], {"a": {"ranks": ["cartservice_cpu"]}})
        self.assertAlmostEqual(summary["service"]["Chance@5"], .3)
        self.assertAlmostEqual(summary["service"]["Lift@5"], .7)


class ExportTests(unittest.TestCase):
    def test_task_export_has_no_answers_or_source_names(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            row = case()
            source = root / "data/raw" / row["case"]
            source.mkdir(parents=True)
            (source / "metrics.parquet").write_bytes(b"fake")
            (root / "data/source-manifest.json").write_text("{}")
            plan = {"files": [{"path": row["case"] + "/metrics.parquet"}]}
            schema = Mock(names=["time", "cartservice_cpu", "frontend_cpu"])
            with patch.object(ev, "validate_local_plan", return_value=(plan, [row])), \
                    patch.object(ev, "verify", return_value=True), \
                    patch.object(ev, "parquet", return_value=Mock(read_schema=Mock(return_value=schema))):
                tasks = ev.export_tasks(root / "data", root / "evaluation")
                with self.assertRaisesRegex(ValueError, "No cases selected"):
                    ev.export_tasks(root / "data", root / "excluded", exclude_tasks=tasks, limit=1)
            self.assertTrue((tasks.parent / "answers.jsonl").exists())
            self.assertFalse((tasks / "answers.jsonl").exists())
            task_text = next(tasks.glob("cases/*/task.json")).read_text()
            self.assertNotIn("root_cause_service", task_text)
            self.assertNotIn(row["case"], task_text)
            self.assertNotIn('"fault"', task_text)
            self.assertEqual(len(ev.load_tasks(tasks)), 1)

    def test_worker_timeout_is_recorded_not_dropped(self):
        import subprocess
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "manifest.json").write_text("{}")
            with patch.object(ev, "load_tasks", return_value={"case-a": {}}), \
                    patch.object(ev.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 1)):
                path = ev.run(tasks, root / "results", "sentinel", 1)
            row = ev.read_jsonl(path)[0]
            self.assertEqual(row["status"], "error")
            self.assertEqual(row["ranks"], [])

    def test_resume_rejects_changed_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tasks = root / "tasks"
            tasks.mkdir()
            (tasks / "manifest.json").write_text("{}")
            with patch.object(ev, "load_tasks", return_value={}):
                ev.run(tasks, root / "results", "sentinel", 5)
                with self.assertRaises(ValueError):
                    ev.run(tasks, root / "results", "sentinel", 10, resume=True)

    @unittest.skipUnless(importlib.util.find_spec("pyarrow") and importlib.util.find_spec("pandas"),
                         "optional evaluation dependencies are not installed")
    def test_sentinel_worker_runs_real_agent_without_labels(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        class FakeLLM:
            calls = []

            def ask(self, role, prompt, schema):
                if role == "Planner":
                    assert "1970-01-01T00:16:40+00:00" in prompt
                    assert '"allowed_tools": ["profile", "metrics"]' in prompt
                    assert '"allowed_tools": ["profile", "logs"]' in prompt
                    # Both observed failure modes: invented ISO dates and raw epoch strings.
                    return {"plan": {"kind": "parallel", "goal": "Inspect metrics", "children": [
                        {"kind": "tool", "goal": "Inspect metrics", "tool": "metrics",
                         "args": {"file": "metric/data.csv", "start": start, "end": end}}
                        for start, end in [("2023-11-17T12:07:12+08:00", "2023-11-17T13:07:12+08:00"),
                                           ("1000", "1600")]] + [
                        {"kind": "tool", "goal": "Read other modality", "tool": tool,
                         "args": {"file": file, "start": "1000", "end": "1600"}}
                        for tool, file in [("logs", "log/data.csv"), ("traces", "trace/data.csv")]]}}
                if role == "Analyzer":
                    return {"summary": "Observed metric change", "complete": True,
                            "confidence": .99, "next_steps": ""}
                if role == "Reporter":
                    assert "Datetimes must be ISO 8601 UTC" in prompt
                    assert "Datetimes must be YYYY-MM-DD HH:MM:SS in UTC+8" not in prompt
                    return {"failures": [{"root cause component": "cartservice"}],
                            "explanation": "Test model response", "limitations": "Synthetic test"}
                raise AssertionError(role)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_dir = root / "task"
            task_dir.mkdir()
            metrics = task_dir / "metrics.parquet"
            # Six healthy samples before the anomaly (the baseline), then a sustained rise.
            pq.write_table(pa.table({"time": list(range(970, 1020, 5)), "cartservice_cpu": [1.] * 6 + [10.] * 4,
                                     "frontend_cpu": [1.] * 10}), metrics)
            pq.write_table(pa.table({"timestamp": [1000], "container_name": ["cartservice"],
                                     "message": ["error: synthetic failure"]}), task_dir / "logs.parquet")
            pq.write_table(pa.table({"time": [1000], "serviceName": ["cartservice"],
                                     "duration": [100.]}), task_dir / "traces.parquet")
            task = {"case_id": "case-test", "system": "Online Boutique", "inject_time": 1000,
                    "standard_window": {"start": 400, "end": 1600}, "candidate_services": ["cartservice", "frontend"],
                    "files": {m: m + ".parquet" for m in ("metrics", "logs", "traces")},
                    "sha256": {p.name: ev.digest(p) for p in task_dir.glob("*.parquet")}}
            (task_dir / "task.json").write_text(json.dumps(task))
            with patch("agent.llm.Ollama", return_value=FakeLLM()):
                ev.worker(task_dir, root / "result/result.json", "sentinel", {"max_rounds": 1})
            result = json.loads((root / "result/result.json").read_text())
            self.assertEqual(result["ranks"], ["cartservice"])
            self.assertEqual(result["status"], "ok")
            agent_dir = root / "result/agent"
            history = json.loads((agent_dir / "call-history.json").read_text())
            self.assertEqual(len(history), 3)  # Duplicate metric calls share the normalized cache key.
            for call in history:
                self.assertEqual(call["args"]["start"], "1970-01-01T00:06:40+00:00")
                self.assertEqual(call["args"]["end"], "1970-01-01T00:26:40+00:00")
                evidence = json.loads((agent_dir / call["evidence_id"] / "result.json").read_text())
                self.assertGreater(evidence["outputs"][0]["result"]["rows_in_window"], 0)
            evidence = json.loads((agent_dir / history[0]["evidence_id"] / "result.json").read_text())
            self.assertEqual(evidence["outputs"][0]["result"]["rows_in_window"], 20)
            self.assertEqual(evidence["outputs"][0]["result"]["findings"][0]["baseline_source"], "pre_anomaly")
            self.assertEqual(json.loads((agent_dir / "supported-components.json").read_text()),
                             {"cartservice": [history[0]["evidence_id"]]})
            self.assertFalse(any(p.name == "ground-truth.json" for p in (root / "result").rglob("*")))


if __name__ == "__main__":
    unittest.main()
