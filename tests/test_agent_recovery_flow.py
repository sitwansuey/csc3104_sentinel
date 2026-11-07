"""Real RCA/Planner-to-recovery handoff, with a scripted model and fake cluster."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from services.coordinator.core import handle
from services.observer.core import detect
from services.rca.core import propose_from_telemetry
from services.verifier.core import inspect_dependencies
from tests.test_rca_integration import Model


class AgentRecoveryFlowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "records").mkdir()
        self.flow = OfflineFlow(self.root / "records")
        self.addCleanup(self.flow.close)
        flow = self.flow
        flow.incident = detect(fixture("observations.json")["failed-rollout"], flow.policy)
        handle(flow.incident, flow.store, flow.bus)
        self.telemetry = self.root / "telemetry"
        self.telemetry.mkdir()
        (self.telemetry / "logs.csv").write_text(
            "timestamp,cmdb_id,message\n"
            "2026-10-10T12:00:30Z,demo-api,Error invalid startup configuration\n",
            encoding="utf-8")

    def plan(self, *, abstain=False, provider=None):
        flow = self.flow
        baseline = flow.baseline

        def rollback(context):
            return {"action": "ROLLBACK", "parameters": {
                key: baseline[key] for key in ("baseline_id", "template_sha256")},
                "rationale": "Restore the measured healthy template for the observed startup failure"}

        proposal = propose_from_telemetry(flow.incident, [baseline], self.telemetry,
            self.root / "diagnosis", provider or rollback, llm=Model(abstain=abstain))
        handle(proposal, flow.store, flow.bus)
        flow.proposal = proposal
        return proposal

    def test_agent_proposal_reaches_guard_executor_and_independent_verifier(self):
        flow = self.flow
        proposal = self.plan()
        finding = inspect_dependencies(proposal, flow.graph, flow.cluster.read(proposal["target"]), now=flow.now)
        handle(finding, flow.store, flow.bus)
        flow.guard()
        self.assertEqual(flow.state(), "AWAITING_APPROVAL")
        self.assertEqual(flow.cluster.writes, [])
        flow.authorize()
        flow.execute()
        self.assertEqual(flow.state(), "VERIFYING")
        self.assertEqual(len(flow.cluster.writes), 1)
        flow.restart()
        flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=timezone.utc)
        flow.verify(fixture("health.json")["stable"])
        self.assertEqual(flow.state(), "RECOVERED")
        self.assertFalse(handle(proposal, flow.store, flow.bus))
        self.assertEqual(flow.execute(), [])
        self.assertEqual(len(flow.cluster.writes), 1)

    def test_abstention_produces_a_recordable_escalation_without_execution(self):
        flow = self.flow
        proposal = self.plan(abstain=True)
        self.assertEqual(proposal["action"], "ESCALATE")
        self.assertEqual(flow.store.action(proposal["action_id"])["proposal"], proposal)
        flow.guard()
        self.assertNotEqual(flow.state(), "READY")
        self.assertEqual(flow.execute(), [])
        self.assertEqual(flow.cluster.writes, [])

    def test_invalid_model_decision_leaves_incident_open_and_does_not_write(self):
        flow = self.flow
        with self.assertRaises(ValueError):
            self.plan(provider=lambda context: {"action": "ROLLBACK", "parameters": {
                "baseline_id": "invented", "template_sha256": "0" * 64}, "rationale": "unsupported"})
        self.assertEqual(flow.state(), "OPEN")
        self.assertEqual(flow.execute(), [])
        self.assertEqual(flow.cluster.writes, [])
