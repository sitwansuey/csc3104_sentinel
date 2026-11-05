import json
from pathlib import Path
import tempfile
import unittest
import pandas as pd
from agent.agent import Agent, validate_report
from agent.tools import Telemetry


TIME = "root cause occurrence datetime"


class ReportingModel:
    def __init__(self, reports):
        self.reports = iter(reports)
        self.calls = []
        self.report_schemas = []

    def ask(self, role, prompt, schema):
        if role == "Planner":
            return {"plan": {"kind": "tool", "tool": "profile", "args": {"file": "metric.csv"}}}
        if role == "Analyzer":
            return {"summary": "uncertain evidence", "complete": False, "confidence": .2, "next_steps": "inspect more"}
        self.report_schemas.append(schema)
        return next(self.reports)


def report(failures):
    return {"failures": failures, "explanation": "best available estimate", "limitations": "uncertain"}


class ReportingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        telemetry = self.root / "telemetry"
        telemetry.mkdir()
        pd.DataFrame({"timestamp": [1], "value": [1]}).to_csv(telemetry / "metric.csv", index=False)
        self.tools = Telemetry(telemetry)

    def tearDown(self):
        self.temp.cleanup()

    def test_default_preserves_uncertain_timestamp_without_candidates(self):
        model = ReportingModel([report([{TIME: "2021-03-06 09:01:00"}])])
        agent = Agent(model, self.tools, {"max_rounds": 1}, self.root / "run")
        prediction = agent.run("Find time", required_fields=[TIME], failure_count=1)
        self.assertEqual(prediction["1"][TIME], "2021-03-06 09:01:00")
        saved = json.loads((self.root / "run/report.json").read_text())
        self.assertIn("Best-effort estimate", saved["limitations"])
        self.assertFalse((self.root / "run/rejected-report.json").exists())
        failures_schema = model.report_schemas[0]["properties"]["failures"]
        self.assertEqual(failures_schema["minItems"], 1)
        self.assertNotIn("enum", failures_schema["items"]["properties"][TIME])

    def test_empty_response_repaired_instead_of_recorded_as_abstention(self):
        model = ReportingModel([report([]), report([{"root cause component": "api"}])])
        agent = Agent(model, self.tools, {"max_rounds": 1}, self.root / "run")
        prediction = agent.run("Find component", required_fields=["root cause component"], failure_count=1)
        self.assertEqual(prediction["1"]["root cause component"], "api")
        self.assertEqual(len(model.report_schemas), 2)
        self.assertTrue((self.root / "run/invalid-report.json").exists())

    def test_repeated_empty_response_is_error_not_successful_abstention(self):
        model = ReportingModel([report([]), report([])])
        agent = Agent(model, self.tools, {"max_rounds": 1}, self.root / "run")
        with self.assertRaisesRegex(ValueError, "Abstention is disabled"):
            agent.run("Find component", required_fields=["root cause component"], failure_count=1)

    def test_multiple_failures_required_in_schema(self):
        model = ReportingModel([report([{TIME: "2021-03-06 09:01:00"}, {TIME: "2021-03-06 09:05:00"}])])
        agent = Agent(model, self.tools, {"max_rounds": 1}, self.root / "run")
        prediction = agent.run("Find two times", required_fields=[TIME], failure_count=2)
        self.assertEqual(len(prediction), 2)
        failures_schema = model.report_schemas[0]["properties"]["failures"]
        self.assertEqual(failures_schema["minItems"], 2)
        self.assertEqual(failures_schema["maxItems"], 2)

    def test_optional_conservative_policy_still_abstains(self):
        model = ReportingModel([report([{TIME: "2021-03-06 09:01:00"}])])
        agent = Agent(model, self.tools, {"max_rounds": 1, "allow_abstention": True}, self.root / "run")
        self.assertEqual(agent.run("Find time", required_fields=[TIME], failure_count=1), {})

    def test_empty_and_blank_fields_rejected(self):
        with self.assertRaises(ValueError):
            validate_report(report([]), [TIME], 1, False)
        with self.assertRaises(ValueError):
            validate_report(report([{"root cause component": " "}]), ["root cause component"], 1, False)

    def test_candidate_vocabulary_enforced_in_schema(self):
        context = "## POSSIBLE ROOT CAUSE REASONS:\n- high CPU usage\n- JVM Out of Memory (OOM) Heap\n## POSSIBLE ROOT CAUSE COMPONENTS:\n- Tomcat01\n- MG01"
        model = ReportingModel([report([{"root cause reason": "JVM Out of Memory (OOM) Heap"}])])
        agent = Agent(model, self.tools, {"max_rounds": 1}, self.root / "run")
        prediction = agent.run("Find reason", context=context, required_fields=["root cause reason"], failure_count=1)
        self.assertEqual(prediction["1"]["root cause reason"], "JVM Out of Memory (OOM) Heap")
        schema = model.report_schemas[0]["properties"]["failures"]["items"]["properties"]["root cause reason"]
        self.assertIn("enum", schema)
        self.assertEqual(schema["enum"], ["JVM Out of Memory (OOM) Heap", "high CPU usage"])

        # Validate that arbitrary sentences are rejected when valid_reasons are provided
        with self.assertRaisesRegex(ValueError, "must be one of"):
            validate_report(report([{"root cause reason": "high CPU and memory load in MG02"}]),
                            ["root cause reason"], 1, False, valid_reasons=["high CPU usage"])
