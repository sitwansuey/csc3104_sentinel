import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd
from agent.agent import Agent
from agent.tools import Telemetry, episodes, error_mask, evidence_time
from agent.evidence import corroborated_onsets, timestamp_rejection


def metric_entry(kpi="cpu", time="2021-03-06 09:05:00", component="api", sustained=True, censored=False):
    return {"evidence_id": kpi, "tool": "metrics", "result": {"file": "metric.csv", "findings": [
        {"component": component, "kpi": kpi, "episodes": [
            {"onset": time, "sustained": sustained, "left_censored": censored, "peak_score": 10}]}]}}


class EvidenceTests(unittest.TestCase):
    def test_utc_evidence_preserves_instant_for_naive_and_aware_times(self):
        for value in ["2024-01-16 04:47:12", "2024-01-16T04:47:12+08:00", "2024-01-15T20:47:12Z"]:
            self.assertEqual(evidence_time(value, True), "2024-01-15T20:47:12+00:00")
        frame = pd.DataFrame({"_time": pd.date_range("2024-01-16 04:45", periods=5, freq="min", tz="Asia/Singapore")})
        result = episodes(frame, pd.Series([0, 5, 5, 5, 0]), utc=True)
        self.assertEqual(result[0]["onset"], "2024-01-15T20:46:00+00:00")
        self.assertEqual(result[0]["end"], "2024-01-15T20:48:00+00:00")

    def test_persistence_and_boundary(self):
        frame = pd.DataFrame({"_time": pd.date_range("2021-03-06 09:00", periods=10, freq="min", tz="Asia/Singapore")})
        result = episodes(frame, pd.Series([5, 0, 5, 0, 0, 5, 5, 5, 0, 0]))
        self.assertEqual(result[0]["onset"], "2021-03-06 09:05:00")
        self.assertTrue(result[0]["sustained"])
        self.assertFalse(result[0]["left_censored"])
        self.assertTrue(any(e["left_censored"] for e in result))
        self.assertEqual(sum(e["sustained"] for e in result), 1)

    def test_gap_breaks_persistence(self):
        frame = pd.DataFrame({"_time": pd.to_datetime(["2021-01-01 09:00", "2021-01-01 09:01", "2021-01-01 09:02", "2021-01-01 10:00", "2021-01-01 10:01"])})
        self.assertFalse(any(e["sustained"] for e in episodes(frame, pd.Series([0, 5, 5, 5, 5]))))

    def test_independent_signals_required(self):
        cpu = metric_entry()
        self.assertEqual(corroborated_onsets([cpu, cpu]), [])
        self.assertTrue(corroborated_onsets([cpu, metric_entry("memory")]))
        self.assertEqual(corroborated_onsets([cpu, metric_entry("memory", component="other")]), [])
        self.assertEqual(corroborated_onsets([cpu, metric_entry("memory", time="2021-03-06 09:15:00")]), [])
        copied = metric_entry()
        copied["result"]["file"] = "copy.csv"
        self.assertEqual(corroborated_onsets([cpu, copied]), [])

    def test_reporter_cannot_override_incomplete_analyzer(self):
        failures = [{"root cause occurrence datetime": "2021-03-06 09:05:00"}]
        candidates = corroborated_onsets([metric_entry(), metric_entry("memory")])
        self.assertIsNotNone(timestamp_rejection(failures, candidates, {"complete": False, "confidence": .9}, .8))
        self.assertIsNotNone(timestamp_rejection(failures, candidates, {"complete": True, "confidence": .3}, .8))
        self.assertIsNone(timestamp_rejection(failures, candidates, {"complete": True, "confidence": .9}, .8))

    def test_incomplete_or_censored_evidence_rejected(self):
        cpu = metric_entry()
        memory = metric_entry("memory")
        memory["result"]["truncated"] = True
        self.assertEqual(corroborated_onsets([cpu, memory]), [])
        self.assertEqual(corroborated_onsets([cpu, metric_entry("memory", censored=True)]), [])
        self.assertEqual(corroborated_onsets([cpu, metric_entry("memory", sustained=False)]), [])

    def test_log_corroboration(self):
        log = {"evidence_id": "log", "tool": "logs", "result": {"file": "log.csv", "events": [
            {"component": "api", "time": "2021-03-06 09:06:00"}]}}
        self.assertEqual(corroborated_onsets([metric_entry(), log])[0]["modalities"], ["logs", "metric"])

    def test_routine_gc_is_not_failure_evidence(self):
        self.assertEqual(error_mask(pd.Series(["GC (Allocation Failure)", "Full GC (Allocation Failure)", "connection refused"])).tolist(), [False, True, True])


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        self.tools = Telemetry(self.telemetry)

    def tearDown(self):
        self.temp.cleanup()

    def test_modality_binding_rejects_cross_tool_calls_even_with_valid_columns(self):
        # Deliberately supply every column: modality must come from the adapter, not schema guessing.
        frame = pd.DataFrame({"timestamp": [1705351632], "cmdb_id": ["api"],
                              "kpi_name": ["cpu"], "value": [1], "message": ["error"], "duration": [1]})
        mapping = {tool + ".csv": tool for tool in ("metrics", "logs", "traces")}
        for name in mapping:
            frame.to_csv(self.telemetry / name, index=False)
        tools = Telemetry(self.telemetry, file_tools=mapping, utc_evidence=True)
        for item in tools.inventory():
            self.assertEqual(item["allowed_tools"], ["profile", mapping[item["file"]]])
        for name, allowed in mapping.items():
            for tool in mapping.values():
                if tool == allowed:
                    self.assertEqual(tools.run(tool, {"file": name})["rows_in_window"], 1)
                else:
                    with self.assertRaisesRegex(ValueError, "Tool/data mismatch"):
                        tools.run(tool, {"file": "./" + name, "message_column": "value", "duration_column": "value"})
            self.assertIn("sample", tools.run("profile", {"file": name}))
        logs = tools.run("logs", {"file": "logs.csv"})
        self.assertEqual(logs["examples"][0]["time"], "2024-01-15T20:47:12+00:00")
        with self.assertRaisesRegex(ValueError, "Tool/data mismatch"):
            tools.run("logs", {"file": "unregistered.csv"})

    def test_duplicates_normalize_defaults_paths_and_timezones(self):
        agent = Agent(None, self.tools, {"max_steps": 1}, self.root / "run")
        first = {"kind": "tool", "tool": "metrics", "args": {"file": "metric.csv", "start": "2021-03-06 09:00:00"}}
        second = {"kind": "tool", "tool": "metrics", "args": {"file": "./metric.csv", "start": "2021-03-06T01:00:00+00:00", "value_column": "value"}}
        with patch.object(self.tools, "run", return_value={"findings": [{"component": "api"}]}) as call:
            self.assertTrue(agent.execute(first)[0])
            ok, output = agent.execute(second)
            self.assertTrue(ok)
            self.assertTrue(output[0]["cache_hit"])
            self.assertEqual(call.call_count, 1)
            self.assertEqual(agent.steps, 1)
            self.assertEqual(len(agent.evidence), 1)

    def test_errors_cached_until_arguments_change(self):
        agent = Agent(None, self.tools, {}, self.root / "run")
        node = {"kind": "tool", "tool": "metrics", "args": {"file": "bad.csv"}}
        with patch.object(self.tools, "run", side_effect=ValueError("bad column")) as call:
            agent.execute(node)
            agent.execute(node)
            self.assertEqual(call.call_count, 1)

    def test_baseline_excludes_later_sustained_shift(self):
        times = pd.date_range("2021-03-06 08:00", periods=120, freq="min")
        frame = pd.DataFrame({"timestamp": times.astype(str), "cmdb_id": "api", "kpi_name": "cpu",
                              "value": [10] * 65 + [100] * 55})
        frame.to_csv(self.telemetry / "metric.csv", index=False)
        result = self.tools.run("metrics", {"file": "metric.csv", "start": "2021-03-06 09:00:00", "end": "2021-03-06 09:30:00"})
        finding = result["findings"][0]
        self.assertEqual(finding["baseline_mean"], 10)
        self.assertEqual(finding["baseline_source"], "preceding_60_minutes")
        self.assertEqual(finding["episodes"][0]["onset"], "2021-03-06 09:05:00")
        self.assertTrue(finding["episodes"][0]["sustained"])

    def test_coverage_adds_logs_before_budget_is_spent(self):
        agent = Agent(None, self.tools, {}, self.root / "run")
        agent.call_history = [{"tool": "metrics", "args": {"file": "day/metric/metric.csv", "start": "2021-03-06 09:00:00", "end": "2021-03-06 09:30:00"}}]
        inventory = [{"file": "day/log/log.csv", "columns": ["timestamp", "cmdb_id", "value"]}]
        with patch.object(self.tools, "run", return_value={"error_count": 1}) as call:
            agent.ensure_coverage(inventory)
            self.assertEqual(call.call_args.args[0], "logs")
            self.assertEqual(agent.steps, 1)

    def test_log_bursts_have_a_baseline_and_onset(self):
        rows = [{"timestamp": str(t), "cmdb_id": "api", "message": "healthy"} for t in pd.date_range("2021-03-06 08:00", periods=60, freq="min")]
        rows += [{"timestamp": "2021-03-06 09:05:00", "cmdb_id": "api", "message": "connection refused"}] * 4
        pd.DataFrame(rows).to_csv(self.telemetry / "log.csv", index=False)
        result = self.tools.run("logs", {"file": "log.csv", "start": "2021-03-06 09:00:00"})
        self.assertEqual(result["events"][0]["time"], "2021-03-06 09:05:00")
        self.assertEqual(result["events"][0]["baseline_errors_per_minute"], 0)

    def test_trace_latency_transition(self):
        frame = pd.DataFrame({"timestamp": pd.date_range("2021-03-06 09:00", periods=30, freq="min").astype(str),
                              "cmdb_id": "api", "duration": [10] * 20 + [100] * 10})
        frame.to_csv(self.telemetry / "trace.csv", index=False)
        result = self.tools.run("traces", {"file": "trace.csv", "start": "2021-03-06 09:00:00"})
        self.assertEqual(result["events"][0]["time"], "2021-03-06 09:20:00")
        self.assertFalse(result["events"][0]["left_censored"])

    def test_supported_timestamp_accepted(self):
        class Model:
            calls = []
            def ask(self, role, prompt, schema):
                if role == "Planner":
                    return {"plan": {"kind": "tool", "tool": "metrics", "args": {"file": "metric.csv"}}}
                if role == "Analyzer":
                    return {"summary": "two sustained KPIs", "complete": True, "confidence": .9, "next_steps": ""}
                return {"failures": [{"root cause occurrence datetime": "2021-03-06 09:05:00"}], "explanation": "corroborated", "limitations": "correlation"}
        pd.DataFrame({"timestamp": [1], "value": [1]}).to_csv(self.telemetry / "metric.csv", index=False)
        result = metric_entry()["result"]
        result["findings"] += metric_entry("memory")["result"]["findings"]
        agent = Agent(Model(), self.tools, {}, self.root / "run")
        with patch.object(self.tools, "run", return_value=result):
            prediction = agent.run("Find time", required_fields=["root cause occurrence datetime"], failure_count=1)
        self.assertEqual(prediction["1"]["root cause occurrence datetime"], "2021-03-06 09:05:00")

    def test_stall_stops_and_unsupported_timestamp_abstains(self):
        class Model:
            calls = []
            def ask(self, role, prompt, schema):
                if role == "Planner":
                    return {"plan": {"kind": "tool", "tool": "profile", "args": {"file": "metric.csv"}}}
                if role == "Analyzer":
                    return {"summary": "insufficient", "complete": False, "confidence": .2, "next_steps": "inspect"}
                return {"failures": [{"root cause occurrence datetime": "2021-03-06 09:01:00"}], "explanation": "earliest spike", "limitations": "unknown"}
        pd.DataFrame({"timestamp": [1], "value": [1]}).to_csv(self.telemetry / "metric.csv", index=False)
        agent = Agent(Model(), self.tools, {"max_rounds": 5, "allow_abstention": True}, self.root / "run")
        prediction = agent.run("Find time", required_fields=["root cause occurrence datetime"], failure_count=1)
        self.assertEqual(prediction, {})
        self.assertEqual(agent.steps, 1)
        self.assertEqual(len(agent.summaries), 2)
        self.assertTrue((self.root / "run/rejected-report.json").exists())
