"""Live observation projection and the model-to-guard proposal boundary."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness.agent_live import AgentPlanningMixin, export_telemetry
from tests.test_rca_integration import Model

ROOT = Path(__file__).resolve().parents[1]


class LiveAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.incident = json.loads((ROOT / "data/sentinel/samples/messages/incident.json").read_text())
        self.baseline = json.loads((ROOT / "data/sentinel/samples/baseline.json").read_text())
        self.observation = {
            "cluster_id": self.incident["target"]["cluster_id"], "captured_at": self.incident["observed_at"],
            "deployment": {"metadata": {"uid": self.incident["target"]["uid"], "resourceVersion": "200"},
                           "spec": {"replicas": 1, "template": {"failure": True}}, "status": {}},
            "logs": [{"text": "2026-10-10T12:00:30Z Error invalid startup configuration"}],
            "events": [{"message": "Back-off restarting failed container"}]}
        (self.root / "primary-0-observation.json").write_text(json.dumps(self.observation))

    def harness(self):
        harness = AgentPlanningMixin()
        harness.directory = self.root
        harness.target = self.incident["target"]
        harness.baseline = self.baseline
        harness.deployment = Mock(return_value=deepcopy(self.observation["deployment"]))
        harness.summary = {}
        harness.event = Mock()
        return harness

    def test_projection_uses_real_observations_and_rejects_other_targets(self):
        out = self.root / "good/telemetry"
        export_telemetry(self.root, self.incident["target"], out)
        with (out / "logs.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["source"] for r in rows}, {"container", "kubernetes_event"})
        self.assertFalse((out / "traces.csv").exists())
        self.observation["deployment"]["metadata"]["uid"] = "other"
        (self.root / "primary-0-observation.json").write_text(json.dumps(self.observation))
        with self.assertRaisesRegex(ValueError, "another target"):
            export_telemetry(self.root, self.incident["target"], self.root / "bad/telemetry")

    def model(self, *, abstain=False):
        baseline = self.baseline

        class LiveModel(Model):
            def ask(self, role, prompt, schema):
                self.calls.append({"role": role, "model": "test-model"})
                if role == "Recovery Planner":
                    return {"action": "ROLLBACK", "parameters": {
                        "baseline_id": baseline["baseline_id"], "template_sha256": baseline["template_sha256"]},
                        "rationale": "Startup configuration failure in observed container logs"}
                return super().ask(role, prompt, schema)
        return LiveModel(abstain=abstain)

    def test_actual_agent_and_planner_produce_bound_proposal(self):
        model = self.model()
        harness = self.harness()
        folder = self.root / "case"
        folder.mkdir()
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=model):
            proposal = harness.build_proposal(self.incident, folder)
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(proposal["parameters"]["baseline_id"], self.baseline["baseline_id"])
        self.assertEqual(proposal["based_on_resource_version"], "200")
        self.assertIn("Recovery Planner", [c["role"] for c in model.calls])
        self.assertTrue((folder / "agent/report.json").is_file())
        self.assertTrue((folder / "model-calls.json").is_file())

    def test_abstention_stops_recovery_without_fallback(self):
        model = self.model(abstain=True)
        folder = self.root / "case"
        folder.mkdir()
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=model):
            with self.assertRaisesRegex(RuntimeError, "ESCALATE"):
                self.harness().build_proposal(self.incident, folder)
        self.assertNotIn("Recovery Planner", [c["role"] for c in model.calls])
        self.assertEqual(json.loads((folder / "model-proposal.json").read_text())["action"], "ESCALATE")


if __name__ == "__main__":
    unittest.main()
