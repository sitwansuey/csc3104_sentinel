"""Live observation projection and the model-to-guard proposal boundary."""
from copy import deepcopy
from datetime import datetime, timedelta
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

    def test_poll_overlap_deduplicates_occurrences_without_collapsing_pod_sources(self):
        at = datetime.fromisoformat(self.incident["observed_at"].replace("Z", "+00:00"))
        observation = deepcopy(self.observation)
        observation["logs"][0].update(pod="pod-1", container="app", pod_uid="uid-1")
        observation["logs"].append({**observation["logs"][0], "pod": "pod-2", "pod_uid": "uid-2"})
        observation["events"][0].update(uid="event-1", lastTimestamp=at.isoformat())
        (self.root / "primary-0-observation.json").write_text(json.dumps(observation))
        for index in range(1, 20):
            observation["captured_at"] = (at + timedelta(seconds=index)).isoformat()
            (self.root / f"poll-{index}-observation.json").write_text(json.dumps(observation))
        export_telemetry(self.root, self.incident["target"], self.root / "case/telemetry")
        with (self.root / "case/telemetry/logs.csv").open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["source_id"] for r in rows if r["source"] == "container"}), 2)
        self.assertTrue(all(r["timestamp"] == at.isoformat() for r in rows))
        manifest = json.loads((self.root / "case/telemetry-export.json").read_text())
        self.assertEqual(manifest["duplicate_logs_removed"], 57)
        self.assertEqual(manifest["metrics"], 60)  # Distinct measured times remain distinct.

    def test_export_excludes_old_incidents_and_future_observations(self):
        at = datetime.fromisoformat(self.incident["observed_at"].replace("Z", "+00:00"))
        for label, offset in (("old", -3600), ("future", 1)):
            observation = deepcopy(self.observation)
            observation["captured_at"] = (at + timedelta(seconds=offset)).isoformat()
            observation["logs"][0]["text"] = observation["captured_at"] + " Error from " + label
            (self.root / (label + "-observation.json")).write_text(json.dumps(observation))
        out = self.root / "case/telemetry"
        export_telemetry(self.root, self.incident["target"], out, incident=self.incident)
        self.assertNotIn("Error from", (out / "logs.csv").read_text())
        manifest = json.loads((out.parent / "telemetry-export.json").read_text())
        self.assertEqual(manifest["snapshots_outside_window"], 2)
        self.assertEqual(manifest["snapshots"], ["primary-0-observation.json"])

    def test_exact_snapshot_replay_and_untimed_tails_do_not_inflate_counts(self):
        observation = deepcopy(self.observation)
        observation["logs"][0]["text"] = "Error without an occurrence timestamp"
        for index in range(4):
            (self.root / f"repeat-{index}-observation.json").write_text(json.dumps(observation))
        (self.root / "primary-0-observation.json").write_text(json.dumps(observation))
        out = self.root / "case/telemetry"
        export_telemetry(self.root, self.incident["target"], out)
        manifest = json.loads((out.parent / "telemetry-export.json").read_text())
        self.assertEqual(manifest["metrics"], 3)
        self.assertEqual(manifest["log_lines"], 2)
        self.assertEqual(manifest["duplicate_metrics_removed"], 12)
        with (out / "logs.csv").open() as stream:
            self.assertTrue(all(r["timestamp_known"] == "False" for r in csv.DictReader(stream)))

    def test_nanosecond_log_timestamps_remain_distinct(self):
        self.observation["logs"][0]["text"] = "\n".join(
            f"2026-10-10T12:00:30.12345678{i}Z Error startup" for i in (1, 2))
        (self.root / "primary-0-observation.json").write_text(json.dumps(self.observation))
        out = self.root / "case/telemetry"
        export_telemetry(self.root, self.incident["target"], out)
        with (out / "logs.csv").open() as stream:
            self.assertEqual(len([r for r in csv.DictReader(stream) if r["source"] == "container"]), 2)

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

    @staticmethod
    def stops(harness):
        return [c for c in harness.event.call_args_list
                if c.args[:2] == (3, "error") and c.args[2].startswith("Agent planning stopped; no repair proposed: ")]

    def assert_visible_stop(self, harness, folder, error_text):
        self.assertEqual(harness.summary["planning"], "Live RCA agent and Ollama Recovery Planner")
        self.assertTrue(harness.summary["model"])
        self.assertIn(error_text, harness.summary["agent_failure"]["error"])
        self.assertEqual(len(self.stops(harness)), 1)
        self.assertEqual(self.stops(harness)[0].kwargs["incident_id"], self.incident["incident_id"])
        self.assertTrue((folder / "model-calls.json").is_file())

    def test_unavailable_model_stops_visibly_without_a_proposal(self):
        model = Mock(calls=[])
        model.ask.side_effect = RuntimeError("Ollama Planner failed for test-model at 127.0.0.1:9: refused")
        harness = self.harness()
        folder = self.root / "unavailable"
        folder.mkdir()
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=model):
            with self.assertRaisesRegex(RuntimeError, "Ollama Planner failed"):
                harness.build_proposal(self.incident, folder)
        self.assert_visible_stop(harness, folder, "RuntimeError: Ollama Planner failed for test-model")
        self.assertEqual(harness.summary["agent_failure"]["model_calls_completed"], 0)
        self.assertFalse((folder / "model-proposal.json").exists())

    def test_abstention_is_a_visible_stop(self):
        harness = self.harness()
        folder = self.root / "abstained"
        folder.mkdir()
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=self.model(abstain=True)):
            with self.assertRaisesRegex(RuntimeError, "ESCALATE"):
                harness.build_proposal(self.incident, folder)
        self.assert_visible_stop(harness, folder, "ESCALATE")

    def test_successful_planning_records_no_failure(self):
        harness = self.harness()
        folder = self.root / "planned"
        folder.mkdir()
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=self.model()):
            harness.build_proposal(self.incident, folder)
        self.assertNotIn("agent_failure", harness.summary)
        self.assertEqual(harness.summary["operator_approval"], "pre-authorized local E2E test")
        self.assertEqual(harness.summary["planning"], "Live RCA agent and Ollama Recovery Planner")
        self.assertEqual(self.stops(harness), [])

    def test_refreshed_version_is_new_intent_and_gets_a_different_action_id(self):
        proposals = []
        for version in ("200", "201"):
            folder = self.root / ("case-" + version)
            folder.mkdir()
            harness = self.harness()
            deployment = deepcopy(self.observation["deployment"])
            deployment["metadata"]["resourceVersion"] = version
            harness.deployment.return_value = deployment
            with patch("experiments.boutique.harness.agent_live.Ollama", return_value=self.model()):
                proposals.append(harness.build_proposal(self.incident, folder))
        self.assertNotEqual(proposals[0]["based_on_resource_version"], proposals[1]["based_on_resource_version"])
        self.assertNotEqual(proposals[0]["action_id"], proposals[1]["action_id"])

    def test_template_change_during_inference_stops_the_proposal(self):
        folder = self.root / "changed-target"
        folder.mkdir()
        harness = self.harness()
        changed = deepcopy(self.observation["deployment"])
        changed["spec"]["template"] = {"another": "configuration"}
        harness.deployment.side_effect = [deepcopy(self.observation["deployment"]), changed]
        with patch("experiments.boutique.harness.agent_live.Ollama", return_value=self.model()):
            with self.assertRaisesRegex(RuntimeError, "Target changed"):
                harness.build_proposal(self.incident, folder)


if __name__ == "__main__":
    unittest.main()
