import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from services.planner.core import propose, retrieve_runbooks
from services.rca.core import diagnose, propose_from_telemetry
from shared.contracts import validate_message

ROOT = Path(__file__).resolve().parents[1]


class Model:
    def __init__(self, abstain=False):
        self.calls = []
        self.abstain = abstain

    def ask(self, role, prompt, schema):
        if role == "Planner":
            return {"plan": {"kind": "tool", "goal": "Read startup errors", "tool": "logs",
                             "args": {"file": "logs.csv", "start": "2026-10-10T11:59:00Z",
                                      "end": "2026-10-10T12:01:00Z"}}}
        if role == "Analyzer":
            return {"summary": "Startup configuration error", "complete": True,
                    "confidence": 0.9, "next_steps": ""}
        return {"failures": [] if self.abstain else [{"root cause component": "demo-api",
                    "root cause reason": "Invalid startup configuration"}],
                "explanation": "Startup error in log evidence", "limitations": "Synthetic test"}


class RCAIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.incident = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text())
        self.baseline = json.loads((ROOT / "data/sentinel/samples/baseline.json").read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        (self.telemetry / "logs.csv").write_text(
            "timestamp,cmdb_id,message\n2026-10-10T12:00:30Z,demo-api,Error invalid startup configuration\n")

    def test_real_engine_and_proposal_handoff(self):
        provider = Mock(return_value={"action": "ROLLBACK", "parameters": {
            "baseline_id": self.baseline["baseline_id"],
            "template_sha256": self.baseline["template_sha256"]}, "rationale": "Startup failed"})
        proposal = propose_from_telemetry(self.incident, [self.baseline], self.telemetry,
                                          self.root / "run", provider, llm=Model())
        validate_message(proposal)
        self.assertEqual(proposal["action"], "ROLLBACK")
        context = provider.call_args.args[0]
        self.assertTrue(context["diagnosis"]["prediction"])
        self.assertTrue(context["runbooks"])
        self.assertTrue((self.root / "run/report.json").exists())
        self.assertTrue((self.root / "run/branch-001/result.json").exists())
        with self.assertRaises(FileExistsError):
            diagnose(self.incident, self.telemetry, self.root / "run", llm=Model())

    def test_known_live_schema_skips_discovery_call_and_reports_tool_progress(self):
        roles, progress = [], []
        class RecordingModel(Model):
            def ask(self, role, prompt, schema):
                roles.append(role)
                return super().ask(role, prompt, schema)
        diagnose(self.incident, self.telemetry, self.root / "run", llm=RecordingModel(), progress=progress.append)
        self.assertEqual(roles, ["Analyzer", "Reporter"])
        tools = [e for e in progress if e["stage"] == "tool"]
        self.assertEqual([e["status"] for e in tools], ["started", "completed"])
        self.assertEqual(tools[-1]["tool"], "logs")

    def test_abstention_escalates_without_calling_provider(self):
        provider = Mock()
        proposal = propose_from_telemetry(self.incident, [self.baseline], self.telemetry,
                                          self.root / "run", provider, llm=Model(abstain=True))
        self.assertEqual(proposal["action"], "ESCALATE")
        provider.assert_not_called()

    def test_missing_baseline_escalates(self):
        provider = Mock()
        proposal = propose(self.incident, [], Mock(), provider)
        self.assertEqual(proposal["action"], "ESCALATE")
        provider.assert_not_called()

    def test_provider_cannot_change_target_or_invent_baseline(self):
        for decision in (
            {"action": "NO_ACTION", "parameters": {}, "rationale": "ok", "target": {}},
            {"action": "ROLLBACK", "parameters": {"baseline_id": "invented",
             "template_sha256": "a" * 64}, "rationale": "bad baseline"},
        ):
            with self.assertRaises(ValueError):
                propose(self.incident, [self.baseline], retrieve_runbooks, lambda ctx: decision)

    def test_rca_provider_cannot_use_future_or_tampered_baseline(self):
        diagnosis = {"incident_id": self.incident["incident_id"], "prediction": {"1": {"reason": "Startup failed"}}}
        for change in ({"healthy_observed_at": "2099-01-01T00:00:00Z"},
                       {"template_sha256": "a" * 64}):
            provider = Mock()
            proposal = propose(self.incident, [dict(self.baseline, **change)], Mock(), provider,
                               diagnosis=diagnosis)
            self.assertEqual(proposal["action"], "ESCALATE")
            provider.assert_not_called()

    def test_rca_provider_cannot_mutate_trusted_baseline(self):
        diagnosis = {"incident_id": self.incident["incident_id"], "prediction": {"1": {"reason": "Startup failed"}}}
        before = json.loads(json.dumps(self.baseline))

        def provider(context):
            context["baselines"][0]["template_sha256"] = "a" * 64
            return {"action": "ROLLBACK", "parameters": {
                key: context["baselines"][0][key] for key in ("baseline_id", "template_sha256")},
                "rationale": "Use a changed baseline"}

        with self.assertRaisesRegex(ValueError, "unavailable or changed"):
            propose(self.incident, [self.baseline], lambda query: {"kp": [{"id": "startup"}]},
                    provider, diagnosis=diagnosis)
        self.assertEqual(self.baseline, before)

    def test_rejects_wrong_incident_diagnosis(self):
        with self.assertRaises(ValueError):
            propose(self.incident, [self.baseline], Mock(), Mock(),
                    diagnosis={"incident_id": "another", "prediction": {}})
