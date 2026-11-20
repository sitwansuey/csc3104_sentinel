"""Healthy-only baselines, dateless timestamps, empty windows, modality budgets and the support gate."""
from pathlib import Path
import tempfile
import unittest

import pandas as pd

from agent.agent import Agent
from agent.datasets.evaluate import trace_columns
from agent.evidence import supported_components
from agent.tools import Telemetry, timestamps

ANOMALY = "2024-01-15T12:10:00+00:00"
START, END = "2024-01-15T12:00:00+00:00", "2024-01-15T12:20:00+00:00"


def epoch(text):
    return int(pd.Timestamp(text).timestamp())


class TelemetryFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.telemetry = Path(self.temp.name) / "telemetry"
        self.telemetry.mkdir()
        self.tools = Telemetry(self.telemetry, utc_evidence=True)

    def tearDown(self):
        self.temp.cleanup()

    def write_metrics(self, values, first="2024-01-15T12:00:00+00:00"):
        times = range(epoch(first), epoch(first) + 60 * len(values), 60)
        pd.DataFrame({"timestamp": list(times), "cmdb_id": "api", "kpi_name": "mem", "value": values}) \
            .to_csv(self.telemetry / "metric.csv", index=False)

    def test_baseline_never_includes_the_faulty_window(self):
        # Ten healthy minutes, then a sustained fault, all inside the window; nothing before it.
        self.write_metrics([10, 11, 10, 9, 10, 11, 10, 9, 10, 11] + [50] * 10)
        plain = self.tools.run("metrics", {"file": "metric.csv", "start": START, "end": END})
        self.assertEqual(plain["findings"], [])
        self.assertEqual(plain["series_without_healthy_baseline"], 1)
        bound = self.tools.run("metrics", {"file": "metric.csv", "start": START, "end": END, "baseline_end": ANOMALY})
        finding = bound["findings"][0]
        self.assertEqual(finding["baseline_source"], "pre_anomaly")
        self.assertEqual(finding["baseline_samples"], 10)
        self.assertAlmostEqual(finding["baseline_mean"], 10.1)
        self.assertEqual(finding["episodes"][0]["onset"], ANOMALY)
        self.assertTrue(finding["episodes"][0]["sustained"])
        self.assertFalse(finding["episodes"][0]["left_censored"])

    def test_empty_window_is_explicit(self):
        self.write_metrics([1] * 5, first="2024-01-15T11:00:00+00:00")
        result = self.tools.run("metrics", {"file": "metric.csv", "start": START, "end": END, "baseline_end": ANOMALY})
        self.assertTrue(result["window_empty"])
        self.assertEqual(result["rows_in_window"], 0)
        self.assertEqual(result["data_range"], {"first": "2024-01-15T11:00:00+00:00", "last": "2024-01-15T11:04:00+00:00"})
        self.assertEqual(result["findings"], [])

    def test_dateless_times_are_never_given_todays_date(self):
        self.assertTrue(timestamps(pd.Series(["15:33", "15:57:01"])).isna().all())
        pd.DataFrame({"time": ["15:33", "15:34"], "cmdb_id": "api", "duration": [1, 2]}) \
            .to_csv(self.telemetry / "trace.csv", index=False)
        with self.assertRaisesRegex(ValueError, "times of day without dates"):
            self.tools.run("traces", {"file": "trace.csv", "time_column": "time", "start": START, "end": END})

    def test_re2_traces_use_epoch_start_time(self):
        mapping = trace_columns(["time", "traceID", "serviceName", "startTime", "duration"])
        self.assertEqual(mapping["startTime"], "timestamp")
        self.assertEqual(mapping["time"], "time_of_day")
        self.assertEqual(trace_columns(["time", "serviceName", "duration"])["time"], "timestamp")
        pd.DataFrame({"timestamp": [epoch(ANOMALY) * 1_000_000], "cmdb_id": "api", "duration": [5]}) \
            .to_csv(self.telemetry / "trace.csv", index=False)
        result = self.tools.run("traces", {"file": "trace.csv", "start": START, "end": END})
        self.assertEqual(result["rows_in_window"], 1)


class SupportGateTests(unittest.TestCase):
    def finding(self, onset, source="pre_anomaly", sustained=True, censored=False, component="api"):
        return {"evidence_id": "branch-001", "tool": "metrics", "result": {"file": "m.csv", "findings": [
            {"component": component, "kpi": "mem", "baseline_source": source, "episodes": [
                {"onset": onset, "sustained": sustained, "left_censored": censored}]}]}}

    def test_support_requires_healthy_baseline_and_post_anomaly_onset(self):
        self.assertEqual(supported_components([self.finding(ANOMALY)], ANOMALY), {"api": ["branch-001"]})
        for entry in [self.finding(ANOMALY, source="full_series_fallback"), self.finding("2024-01-15T12:02:00+00:00"),
                      self.finding(ANOMALY, sustained=False), self.finding(ANOMALY, censored=True)]:
            self.assertEqual(supported_components([entry], ANOMALY), {})
        empty = {"evidence_id": "b", "tool": "metrics", "result": {"window_empty": True, "findings": []}}
        self.assertEqual(supported_components([empty], ANOMALY), {})


class AgentFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.telemetry = self.root / "telemetry"
        (self.telemetry / "metric").mkdir(parents=True)
        pd.DataFrame({"timestamp": [epoch(ANOMALY)], "cmdb_id": ["api"], "kpi_name": ["mem"], "value": [1]}) \
            .to_csv(self.telemetry / "metric/data.csv", index=False)
        self.tools = Telemetry(self.telemetry, file_tools={"metric/data.csv": "metrics"}, utc_evidence=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_unavailable_modality_costs_no_budget_and_is_hidden(self):
        agent = Agent(None, self.tools, {}, self.root / "run")
        ok, output = agent.execute({"kind": "tool", "tool": "logs", "args": {"file": "metric/data.csv"}})
        self.assertFalse(ok)
        self.assertIn("Tool/data mismatch", output[0]["error"])
        self.assertEqual(agent.steps, 0)
        self.assertEqual(len(agent.rejected_calls), 1)
        catalog = agent.catalog(self.tools.inventory())
        self.assertNotIn("\nlogs(", catalog)
        self.assertNotIn("\ntraces(", catalog)
        self.assertIn("metrics(", catalog)
        self.assertIn("Unavailable in this case", catalog)

    def test_reporter_abstains_without_supported_component(self):
        class Model:
            calls = []
            asked = []

            def ask(self, role, prompt, schema):
                self.asked.append(role)
                if role == "Planner":
                    return {"plan": {"kind": "tool", "tool": "metrics", "args": {"file": "metric/data.csv",
                            "start": START, "end": END, "baseline_end": ANOMALY}}}
                if role == "Analyzer":
                    return {"summary": "no evidence", "complete": False, "confidence": .1, "next_steps": "none"}
                return {"failures": [{"root cause component": "api"}], "explanation": "guess", "limitations": ""}
        model = Model()
        agent = Agent(model, self.tools, {"allow_abstention": True, "max_rounds": 1}, self.root / "run")
        agent.support_not_before = ANOMALY
        prediction = agent.run("Find the service", context="## POSSIBLE ROOT CAUSE COMPONENTS\n- api\n- db",
                               required_fields=["root cause component"], failure_count=1)
        self.assertEqual(prediction, {})
        self.assertNotIn("Reporter", model.asked)


if __name__ == "__main__":
    unittest.main()
