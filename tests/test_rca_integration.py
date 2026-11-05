import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from services.planner.core import propose
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
                propose(self.incident, [self.baseline], lambda q: [], lambda ctx: decision)

    def test_rejects_wrong_incident_diagnosis(self):
        with self.assertRaises(ValueError):
            propose(self.incident, [self.baseline], Mock(), Mock(),
                    diagnosis={"incident_id": "another", "prediction": {}})
