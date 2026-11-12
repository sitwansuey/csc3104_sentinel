"""Live observation projection and the model-to-guard proposal boundary.

Agent mode plans through the durable Planner journal (experiments/boutique/
harness/durable_case.py). These tests run the real harness method, agent,
journal and Coordinator with a scripted model and no cluster; they stop at the
first Role 6 dependency check, which needs a cluster.
"""
from copy import deepcopy
from datetime import datetime, timedelta
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness.agent_live import AgentPlanningMixin, export_telemetry, names_target
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from services.planner.store import PlannerStore
from tests.support.fake_ollama import FakeOllama, Reply
from tests.test_rca_integration import Model

ROOT = Path(__file__).resolve().parents[1]
MODEL = "services.planner.agent_provider.Ollama"


class ReachedDependencyCheck(Exception):
    """Planning delivered a ROLLBACK and the harness asked Role 6 about it."""


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

        # Durable backoff is real time in the harness; keep these tests fast.
        for name, value in (("RETRY_SECONDS", .01), ("POLL_SECONDS", .01)):
            fast = patch("experiments.boutique.harness.durable_case." + name, value)
            fast.start()
            self.addCleanup(fast.stop)

    def harness(self, directory=None, *, expect="recovered"):
        harness = AgentPlanningMixin()
        harness.directory = directory or self.root
        if directory:
            directory.mkdir()
            (directory / "primary-0-observation.json").write_text(json.dumps(self.observation))
        harness.target = self.incident["target"]
        harness.baseline = self.baseline
        harness.deployment = Mock(return_value=deepcopy(self.observation["deployment"]))
        harness.summary = {}
        harness.event = Mock()
        harness.phase = Mock()
        harness.check = Mock()
        harness.emergency_restore = Mock(return_value=True)
        harness.dependency_result = Mock(side_effect=ReachedDependencyCheck)
        harness.expect = expect
        harness.store = SQLiteStore(harness.directory / "coordinator.sqlite")
        self.addCleanup(harness.store.close)
        harness.bus = LocalEventBus()
        handle(self.incident, harness.store, harness.bus)
        return harness

    def recover(self, harness, model):
        with patch(MODEL, return_value=model):
            harness.recover_incident(self.incident)

    def planned(self, harness, model):
        """Run to the first dependency check; return the delivered proposal."""
        with self.assertRaises(ReachedDependencyCheck):
            self.recover(harness, model)
        return harness.dependency_result.call_args.args[0]

    def journal(self, harness):
        with PlannerStore(harness.directory / "approved-rollback/planner.db") as journal:
            return journal.get(self.incident["incident_id"])

    def state(self, harness):
        return harness.store.incident(self.incident["incident_id"])["state"]

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
        proposal = self.planned(harness, model)
        folder = self.root / "approved-rollback"
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(proposal["parameters"]["baseline_id"], self.baseline["baseline_id"])
        self.assertEqual(proposal["based_on_resource_version"], "200")
        self.assertEqual(harness.store.action(proposal["action_id"])["proposal"], proposal)
        self.assertIn("Recovery Planner", [c["role"] for c in model.calls])
        self.assertTrue((folder / "attempt-1/agent/report.json").is_file())
        self.assertEqual({c["planning_attempt"] for c in json.loads((folder / "model-calls.json").read_text())}, {1})
        status = json.loads((folder / "planning-status.json").read_text())
        self.assertEqual((status["attempts"], status["delivered"], status["proposal"]), (1, 1, proposal))
        self.assertEqual(status["attempt_usage"][0]["usage"]["planning_attempt"], 1)
        # planning-status.json and summary.json both record who decided, under which policy.
        decision = status["attempt_usage"][0]["usage"]["decision"]
        self.assertEqual((decision["policy"], decision["decided_by"], decision["eligible"]), ("gated", "model", True))
        self.assertEqual(harness.summary["planning_decision"], decision)
        self.assertEqual(harness.summary["decision_policy"], "gated")
        self.assertEqual(json.loads((folder / "proposal.json").read_text()), proposal)
        harness.phase.assert_called_with("proposal_saved", "approved-rollback")

    def test_crash_after_save_before_delivery_replays_without_another_model_call(self):
        model = self.model()
        harness = self.harness()
        with patch("runtime.planner_pipeline.record_message", side_effect=OSError("lost result")):
            with self.assertRaises(OSError):
                self.recover(harness, model)
        saved = self.journal(harness)
        self.assertEqual((saved["delivered"], self.state(harness)), (0, "OPEN"))
        calls = len(model.calls)
        # A restarted harness: new Coordinator connection, same run folder and journal.
        harness.store.close()
        harness.store = SQLiteStore(self.root / "coordinator.sqlite")
        self.addCleanup(harness.store.close)
        self.assertEqual(self.planned(harness, model), saved["proposal"])
        self.assertEqual(len(model.calls), calls)
        self.assertEqual((self.journal(harness)["attempts"], self.journal(harness)["delivered"]), (1, 1))

    def test_abstention_stops_recovery_without_fallback(self):
        model = self.model(abstain=True)
        harness = self.harness()
        with self.assertRaisesRegex(RuntimeError, "stopped at NEEDS_REVIEW; expected RECOVERED"):
            self.recover(harness, model)
        self.assertNotIn("Recovery Planner", [c["role"] for c in model.calls])
        saved = json.loads((self.root / "approved-rollback/model-proposal.json").read_text())
        self.assertEqual(saved["action"], "ESCALATE")
        self.assertEqual(self.state(harness), "NEEDS_REVIEW")
        self.assertEqual((self.journal(harness)["error_code"], self.journal(harness)["attempts"]), ("ABSTAINED", 1))
        harness.dependency_result.assert_not_called()
        harness.emergency_restore.assert_not_called()  # On failure, run() restores as administrator.

    @staticmethod
    def stops(harness):
        return [c for c in harness.event.call_args_list
                if c.args[:2] == (3, "error") and c.args[2].startswith("Agent planning stopped; no repair proposed: ")]

    def assert_visible_stop(self, harness, error_text):
        self.assertEqual(harness.summary["planning"], "Live RCA agent and Ollama Recovery Planner")
        self.assertTrue(harness.summary["model"])
        self.assertIn(error_text, harness.summary["agent_failure"]["error"])
        self.assertEqual(harness.summary["agent_failure"]["incident_state"], "NEEDS_REVIEW")
        self.assertEqual(len(self.stops(harness)), 1)
        self.assertEqual(self.stops(harness)[0].kwargs["incident_id"], self.incident["incident_id"])
        self.assertTrue((harness.directory / "approved-rollback/model-calls.json").is_file())

    def test_unavailable_model_retries_then_escalates_to_needs_review(self):
        model = Mock(calls=[])
        model.ask.side_effect = RuntimeError("Ollama Planner failed for test-model at 127.0.0.1:9: refused")
        harness = self.harness()
        with self.assertRaisesRegex(RuntimeError, "stopped at NEEDS_REVIEW"):
            self.recover(harness, model)
        self.assert_visible_stop(harness, "ESCALATE MODEL_UNAVAILABLE after 3 attempt(s): "
                                          "RuntimeError: Ollama Planner failed for test-model")
        self.assertEqual(harness.summary["agent_failure"]["model_calls_completed"], 0)
        self.assertEqual(model.ask.call_count, 3)
        self.assertEqual(self.state(harness), "NEEDS_REVIEW")
        saved = self.journal(harness)
        self.assertEqual((saved["attempts"], saved["error_code"], saved["delivered"]), (3, "MODEL_UNAVAILABLE", 1))
        self.assertEqual(harness.summary["planning_journal"],
                         {"attempts": 3, "error_code": "MODEL_UNAVAILABLE", "delivered": 1})
        self.assertNotIn("refused", json.dumps(saved))
        harness.dependency_result.assert_not_called()

    def test_abstention_is_a_visible_stop(self):
        harness = self.harness()
        with self.assertRaisesRegex(RuntimeError, "NEEDS_REVIEW"):
            self.recover(harness, self.model(abstain=True))
        self.assert_visible_stop(harness, "ESCALATE ABSTAINED after 1 attempt(s)")

    def test_successful_planning_records_no_failure(self):
        harness = self.harness()
        self.planned(harness, self.model())
        self.assertNotIn("agent_failure", harness.summary)
        # Planning never claims approval; the run's approver records it through approval_http.
        self.assertNotIn("operator_approval", harness.summary)
        self.assertEqual(harness.summary["planning"], "Live RCA agent and Ollama Recovery Planner")
        self.assertEqual(harness.summary["expected_outcome"], "recovered")
        self.assertEqual(self.stops(harness), [])

    def test_expected_escalation_passes_after_an_administrator_restore(self):
        harness = self.harness(expect="escalated")
        self.recover(harness, self.model(abstain=True))
        harness.emergency_restore.assert_called_once_with()
        harness.check.assert_called_once()
        self.assertEqual(harness.check.call_args.kwargs, {"error_code": "ABSTAINED", "attempts": 1})
        self.assertEqual(self.state(harness), "NEEDS_REVIEW")
        # summary.json must say so: this PASS is not a recovery.
        self.assertEqual(harness.summary["expected_outcome"], "escalated")
        self.assertEqual(len(self.stops(harness)), 1)

    def test_expected_escalation_fails_if_the_fault_cannot_be_removed(self):
        harness = self.harness(expect="escalated")
        harness.emergency_restore.return_value = False
        with self.assertRaisesRegex(RuntimeError, "Administrator restore"):
            self.recover(harness, self.model(abstain=True))

    def test_refreshed_version_is_new_intent_and_gets_a_different_action_id(self):
        proposals = []
        for version in ("200", "201"):
            harness = self.harness(self.root / ("case-" + version))
            deployment = deepcopy(self.observation["deployment"])
            deployment["metadata"]["resourceVersion"] = version
            harness.deployment.return_value = deployment
            proposals.append(self.planned(harness, self.model()))
        self.assertNotEqual(proposals[0]["based_on_resource_version"], proposals[1]["based_on_resource_version"])
        self.assertNotEqual(proposals[0]["action_id"], proposals[1]["action_id"])

    def test_template_change_during_inference_escalates_without_a_proposal(self):
        harness = self.harness()
        changed = deepcopy(self.observation["deployment"])
        changed["spec"]["template"] = {"another": "configuration"}
        harness.deployment.side_effect = [deepcopy(self.observation["deployment"]), changed]
        with self.assertRaisesRegex(RuntimeError, "NEEDS_REVIEW"):
            self.recover(harness, self.model())
        self.assertEqual(self.journal(harness)["error_code"], "TARGET_CHANGED")
        self.assert_visible_stop(harness, "ESCALATE TARGET_CHANGED after 1 attempt(s)")
        harness.dependency_result.assert_not_called()

    def test_component_naming_variants_still_identify_only_the_target(self):
        for component in ("shippingservice", "shippingservice container", "ShippingService",
                          "deployment/shippingservice", "sentinel-boutique-connected/shippingservice",
                          "the shippingservice pod", '"shippingservice"', "shippingservice-6d94cdf884-gzh9t"):
            with self.subTest(accepted=component):
                self.assertTrue(names_target(component, "shippingservice"))
        for component in ("frontend", "frontend calling shippingservice", "shippingservice and frontend",
                          "shippingservicex", "shipping service", "shippingservice-6d94cdf884",
                          "shippingservice:50051", "container", "", None, 42):
            with self.subTest(rejected=component):
                self.assertFalse(names_target(component, "shippingservice"))

    def test_pod_container_reference_names_the_target_only_for_its_own_containers(self):
        # Seen live on 11 November 2026: "<pod>/<container>", with the target's container "server".
        live = "shippingservice-85576d7678-5hb44/server"
        for component in (live, "shippingservice/server", "the shippingservice-85576d7678-5hb44/server container"):
            with self.subTest(accepted=component):
                self.assertTrue(names_target(component, "shippingservice", ["server"]))
        self.assertFalse(names_target(live, "shippingservice"))  # Unknown container: not trusted.
        for component in ("shippingservice-85576d7678-5hb44/other", "frontend-6d94cdf884-gzh9t/server",
                          "frontend/server", "a/shippingservice/server", "shippingservice-85576d7678/server",
                          "shippingservice-85576d7678-5hb44/server and frontend"):
            with self.subTest(rejected=component):
                self.assertFalse(names_target(component, "shippingservice", ["server"]))

    def model_naming(self, component, times=None):
        """A model whose Reporter names component, in its first `times` answers (all if None)."""
        base = self.model()

        class NamingModel(type(base)):
            reports = 0

            def ask(self, role, prompt, schema):
                answer = super().ask(role, prompt, schema)
                if role not in ("Planner", "Analyzer", "Recovery Planner") and answer.get("failures"):
                    self.reports += 1
                    if times is None or self.reports <= times:
                        answer["failures"][0]["root cause component"] = component
                return answer
        return NamingModel()

    def reporter_calls(self, model):
        return [c["role"] for c in model.calls].count("Reporter")

    def test_descriptive_component_name_still_reaches_the_recovery_planner(self):
        # The code-owned vocabulary rejects the variant once; the corrected exact name proceeds.
        model = self.model_naming(self.incident["target"]["name"] + " container", times=1)
        proposal = self.planned(self.harness(), model)
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.reporter_calls(model), 2)
        self.assertIn("Recovery Planner", [c["role"] for c in model.calls])

    def test_pod_container_reference_reaches_the_recovery_planner(self):
        container = self.baseline["pod_template"]["spec"]["containers"][0]["name"]
        model = self.model_naming(self.incident["target"]["name"] + "-85576d7678-5hb44/" + container, times=1)
        proposal = self.planned(self.harness(), model)
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.reporter_calls(model), 2)
        self.assertIn("Recovery Planner", [c["role"] for c in model.calls])

    def test_off_vocabulary_component_is_corrected_once_then_abstains(self):
        model = self.model_naming(self.incident["target"]["name"] + " container")
        harness = self.harness()
        with self.assertRaisesRegex(RuntimeError, "NEEDS_REVIEW"):
            self.recover(harness, model)
        self.assertEqual(self.reporter_calls(model), 2)
        self.assertNotIn("Recovery Planner", [c["role"] for c in model.calls])
        self.assertEqual(self.journal(harness)["error_code"], "ABSTAINED")
        agent = self.root / "approved-rollback/attempt-1/agent"
        self.assertTrue((agent / "invalid-report.json").is_file())
        self.assertTrue((agent / "invalid-correction.json").is_file())

    def test_diagnosis_naming_another_component_still_escalates(self):
        # A vocabulary name (the reviewed Boutique topology) that is not the target. Live
        # telemetry names only the target, so no cited result supports it: the agent abstains.
        model = self.model_naming("shippingservice")
        harness = self.harness()
        with self.assertRaisesRegex(RuntimeError, "NEEDS_REVIEW"):
            self.recover(harness, model)
        self.assertNotIn("Recovery Planner", [c["role"] for c in model.calls])
        self.assertEqual(self.journal(harness)["error_code"], "ABSTAINED")
        summary = json.loads((self.root / "approved-rollback/attempt-1/agent/execution-summary.json").read_text())
        self.assertIn("does not name the diagnosed component", summary["citation_refusal"])


class AgentApprovalSessionTests(unittest.TestCase):
    """Agent mode plans through the durable journal AND approves through the authenticated session.

    The real AgentPlanningMixin, recovery Experiment, durable case, Coordinator,
    Guard, approval_http child process (LocalApprovalAuthority) and Executor core
    run; the model and Kubernetes are scripted fakes, and the Verifier outcome is
    a fixed passing record so the run can reach RECOVERED offline.
    """

    def setUp(self):
        from experiments.recovery import live as recovery
        from tests.test_approval_session import real_now, sample, sample_delta
        from tests.support.executor_fake import FakeDeploymentAPI

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        delta = sample_delta()
        self.sample, self.real_now = (lambda name: sample(name, delta)), real_now
        self.incident = self.sample("messages/incident.json")
        self.baseline, self.policy = self.sample("baseline.json"), self.sample("policy.json")
        self.cluster = FakeDeploymentAPI(self.sample("observations.json")["failed-rollout"][-1], clock=real_now)
        self.cluster.stats = {}
        self.built = []  # approval_reader of each Executor adapter set the harness built
        self.outcomes = []
        for patcher in (patch("subprocess.run", side_effect=AssertionError("cluster command attempted")),
                        patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch.object(recovery, "build_adapters", side_effect=self.build_adapters),
                        patch("experiments.boutique.harness.durable_case.RETRY_SECONDS", .01),
                        patch("experiments.boutique.harness.durable_case.POLL_SECONDS", .01)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.kind = type("OfflineAgentExperiment", (AgentPlanningMixin, recovery.Experiment), {
            "deployment": lambda lab: self.cluster.read(lab.target)["deployment"],
            "dependency_result": lambda lab, proposal: self.dependencies(proposal),
            "complete_recovery": lambda lab, folder, proposal, execution: self.verified(lab, proposal, execution),
            "event": lambda lab, *args, **kwargs: None})

    def dependencies(self, proposal):
        return dict(self.sample("messages/dependency-result.json"), incident_id=proposal["incident_id"],
                    action_id=proposal["action_id"])

    def build_adapters(self, **options):
        from runtime.executor_pipeline import GuardAuthorizer
        from services.executor.core import ExecutionAdapters
        from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
        self.built.append(options["approval_reader"])
        authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
                                     dependency_reader=self.dependencies, approval_reader=options["approval_reader"])
        return ExecutionAdapters(ledger=SQLiteExecutionLedger(options["ledger"]),
                                 coordination=LocalTargetCoordinator(options["locks"]), cluster=self.cluster,
                                 authorizer=authorizer, clock=self.real_now)

    def verified(self, lab, proposal, execution):
        """A fixed passing Verifier outcome for the applied action, measured after its write."""
        from experiments.recovery.live import utc
        start = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
        outcome = dict(self.sample("messages/outcome.json"), incident_id=proposal["incident_id"],
                       action_id=proposal["action_id"], window_start=utc(start),
                       window_end=utc(max(self.real_now(), start + timedelta(seconds=1))))
        handle(outcome, lab.store, lab.bus)
        self.outcomes.append(outcome)

    def experiment(self, *, expect="recovered", approval="auto-lab"):
        lab = self.kind(self.root / "run")
        self.addCleanup(lab.store.close)
        self.addCleanup(lab.close_approval_authority)
        lab.target, lab.baseline, lab.policy, lab.expect = self.incident["target"], self.baseline, self.policy, expect
        lab.emergency_restore = Mock(return_value=True)
        if approval:
            lab.configure_approval(approval)
        else:  # What main() records for --expect escalated.
            lab.approval_not_requested("no approval was requested: --expect escalated")
        observation = {"cluster_id": lab.target["cluster_id"], "captured_at": self.incident["observed_at"],
                       "deployment": lab.deployment(),
                       "logs": [{"text": self.incident["observed_at"] + " Error invalid startup configuration"}],
                       "events": []}
        (lab.directory / "primary-0-observation.json").write_text(json.dumps(observation))
        handle(self.incident, lab.store, lab.bus)
        return lab

    def model(self, *, abstain=False):
        return LiveAgentTests.model(Mock(baseline=self.baseline), abstain=abstain)

    def recover(self, lab, model):
        with patch(MODEL, return_value=model):
            lab.recover_incident(self.incident)

    def state(self, lab):
        return lab.store.incident(self.incident["incident_id"])["state"]

    def test_agent_success_approves_through_the_session_and_records_the_strategy(self):
        from runtime.approval_http import ApprovalClient
        from runtime.approval_session import AUTOMATED_LAB, LAB_EXECUTOR
        lab = self.experiment()
        self.recover(lab, self.model())
        self.assertEqual(self.state(lab), "RECOVERED")
        action_id = lab.store.incident(self.incident["incident_id"])["active_action_id"]
        # The approval was created through approval_http, under the automated-lab identity.
        self.assertTrue(lab.store.has_managed_approval(action_id))
        saved = json.loads((lab.directory / "approved-rollback/approval.json").read_text(encoding="utf-8"))
        self.assertEqual((saved["action_id"], saved["operator_id"]), (action_id, AUTOMATED_LAB))
        summary = lab.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["via"], summary["human"], summary["identity"]),
                         ("auto-lab", "approval_http", False, AUTOMATED_LAB))
        self.assertEqual([item["action_id"] for item in summary["approvals"]], [action_id])
        self.assertIn("no person decided", summary["decided_by"])
        self.assertNotIn("pre-authori", json.dumps(lab.summary))
        # The Executor's final lookup reads that approval through ApprovalClient, and it wrote once.
        self.assertEqual([(type(reader), reader.identity) for reader in self.built], [(ApprovalClient, LAB_EXECUTOR)])
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertEqual(lab.summary["planning_journal"], {"attempts": 1, "error_code": None, "delivered": 1})
        lab.close_approval_authority()
        self.assertTrue(lab.summary["operator_approval"]["authority_stopped"])

    def test_escalated_agent_run_creates_no_approval_and_starts_no_session(self):
        lab = self.experiment(expect="escalated", approval=None)
        self.recover(lab, self.model(abstain=True))
        self.assertEqual(self.state(lab), "NEEDS_REVIEW")
        lab.emergency_restore.assert_called_once_with()
        action_id = lab.store.incident(self.incident["incident_id"])["active_action_id"]
        self.assertIsNone(lab.store.approval(action_id))
        self.assertIsNone(lab.authority)
        self.assertFalse((lab.directory / "approval-identities.json").exists())
        self.assertEqual(lab.summary["operator_approval"]["mode"], "not-requested")
        self.assertEqual((self.built, self.cluster.writes), ([], []))

    def test_escalated_run_refuses_a_proposed_repair_without_asking(self):
        lab = self.experiment(expect="escalated", approval=None)
        with self.assertRaisesRegex(RuntimeError, "stopped at AWAITING_APPROVAL; expected NEEDS_REVIEW"):
            self.recover(lab, self.model())
        self.assertIsNone(lab.authority)
        self.assertFalse((lab.directory / "approval-identities.json").exists())
        self.assertFalse(lab.store.pending_routes("executor.request"))
        self.assertEqual((self.built, self.cluster.writes), ([], []))

    def test_rejected_approval_in_agent_mode_gives_zero_writes(self):
        lab = self.experiment(approval="reject")
        with self.assertRaisesRegex(RuntimeError, "did not approve .*no Executor write attempted"):
            self.recover(lab, self.model())
        self.assertEqual(self.state(lab), "AWAITING_APPROVAL")
        action_id = lab.store.incident(self.incident["incident_id"])["active_action_id"]
        self.assertFalse(lab.store.has_managed_approval(action_id))
        self.assertFalse(lab.store.pending_routes("executor.request"))
        self.assertEqual((self.built, self.cluster.writes), ([], []))
        summary = lab.summary["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"], summary["approvals"]), ("reject", None, []))
        self.assertEqual([item["action_id"] for item in summary["refusals"]], [action_id])


class ExpectOptionTests(unittest.TestCase):
    def test_expect_escalated_requires_agent_mode(self):
        from experiments.boutique.harness import live
        errors = io.StringIO()
        with patch("sys.argv", ["test_boutique_live.py", "--expect", "escalated"]), patch("sys.stderr", errors), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            with self.assertRaises(SystemExit) as stopped:
                live.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertIn("--expect escalated requires --agent", errors.getvalue())

    def test_expect_escalated_needs_no_terminal_and_records_that_no_approval_was_requested(self):
        from experiments.boutique.harness import live
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run"
        with patch("sys.argv", ["harness", "--run-dir", str(run_dir), "--agent", "--expect", "escalated"]), \
                patch("sys.stdin", io.StringIO()), patch("sys.stderr", io.StringIO()), \
                patch("sys.stdout", io.StringIO()), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")), \
                patch("shared.provenance.collect", return_value={"source_revision": "test"}), \
                patch.object(live, "tool_versions", return_value={}), \
                patch.object(AgentPlanningMixin, "prepare", side_effect=RuntimeError("stop before the model")):
            self.assertEqual(live.main(), 1)
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))["operator_approval"]
        self.assertEqual((summary["mode"], summary["identity"], summary["via"], summary["human"]),
                         ("not-requested", None, None, False))
        self.assertIn("--expect escalated", summary["decided_by"])
        self.assertFalse((run_dir / "approval-identities.json").exists())


class ModelPreflightTests(unittest.TestCase):
    """The model is checked and loaded before any cluster change or fault."""

    def setUp(self):
        from experiments.boutique.harness.live import BoutiqueExperiment
        self.base = BoutiqueExperiment
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.server = FakeOllama()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)
        # Any cluster or container command means the run went too far.
        touched = patch("subprocess.run", side_effect=AssertionError("cluster command attempted"))
        touched.start()
        self.addCleanup(touched.stop)
        # Provenance runs git and tool version commands; keep it out of this guard.
        stamped = patch("shared.provenance.collect", return_value={"source_revision": "test"})
        stamped.start()
        self.addCleanup(stamped.stop)

    def experiment(self, **config):
        kind = type("AgentBoutiqueExperiment", (AgentPlanningMixin, self.base), {})
        experiment = kind(self.root / "run")
        path = self.root / "model.json"
        path.write_text(json.dumps({"base_url": self.server.url, "model": "fixture-model", "timeout": 5,
                                    "warm_up_timeout_seconds": 2, **config}))
        experiment.agent_config = path
        experiment.inject_fault = Mock()
        experiment.event = Mock()
        return experiment

    def assert_stopped_before_injection(self, stage, pattern, **config):
        experiment = self.experiment(**config)
        with patch.object(self.base, "prepare") as cluster_prepare:
            with self.assertRaisesRegex(Exception, pattern):
                experiment.run()
        cluster_prepare.assert_not_called()
        experiment.inject_fault.assert_not_called()
        summary = json.loads((experiment.directory / "summary.json").read_text())
        self.assertEqual(summary["status"], "FAILED")
        self.assertEqual(summary["agent_failure"]["stage"], stage)
        self.assertFalse(summary["agent_failure"]["fault_injected"])
        self.assertRegex(summary["agent_failure"]["error"], pattern)
        self.assertNotIn("operator_emergency_restore", summary)
        self.assertFalse((experiment.directory / "model-runtime.json").exists())
        errors = [c for c in experiment.event.call_args_list if c.args[:2] == (3, "error")]
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].args[2].startswith("Agent model check failed; no fault injected: "))
        return experiment

    def test_warm_up_failure_stops_before_inject_fault(self):
        self.server.chat = Reply({"error": "model failed to load"}, status=500)
        self.assert_stopped_before_injection(
            "warm_up", "Ollama warm-up failed for fixture-model at 127.0.0.1:[0-9]+: HTTP 500 .*model failed to load")

    def test_missing_model_stops_before_inject_fault_without_a_chat(self):
        self.assert_stopped_before_injection("preflight", "fixture-missing at .*model is not installed",
                                             model="fixture-missing")
        self.assertEqual(self.server.paths("/api/chat"), [])

    def test_unreachable_ollama_stops_before_inject_fault(self):
        with FakeOllama() as closed:
            url = closed.url
        self.assert_stopped_before_injection("preflight", "Ollama preflight failed for fixture-model at "
                                             + url.split("//")[1], base_url=url)

    def test_invalid_model_config_stops_before_inject_fault(self):
        self.assert_stopped_before_injection("preflight", "base_url", base_url="http://ollama.example:11434")

    def test_invalid_planning_budget_stops_before_inject_fault_without_a_model_call(self):
        base = self.root
        for number, budget in enumerate((0, -1, "600", 86400)):
            with self.subTest(budget=budget):
                self.root = base / str(number)  # one run folder per case
                self.root.mkdir()
                self.assert_stopped_before_injection("preflight", "planning_budget_seconds",
                                                     planning_budget_seconds=budget)
                self.assertEqual(self.server.paths(), [])

    def test_invalid_decision_policy_stops_before_inject_fault_without_a_model_call(self):
        base = self.root
        for number, config in enumerate(({"decision_policy": "always-rollback"}, {"max_change_age_seconds": 0},
                                         {"consult_model_when_ineligible": "yes"})):
            with self.subTest(config=config):
                self.root = base / ("policy-" + str(number))
                self.root.mkdir()
                self.assert_stopped_before_injection("preflight", "decision_policy|max_change_age_seconds|"
                                                     "consult_model_when_ineligible", **config)
                self.assertEqual(self.server.paths(), [])

    def test_invalid_topology_stops_before_inject_fault_without_a_model_call(self):
        base = self.root
        for number, text in enumerate(('{"nodes": ["Shipping Service"]}', '{"nodes": []}', "[]")):
            with self.subTest(topology=text):
                self.root = base / ("topology-" + str(number))
                self.root.mkdir()
                topology = self.root / "dependencies.json"
                topology.write_text(text, encoding="utf-8")
                with patch("experiments.boutique.harness.agent_live.TOPOLOGY", topology):
                    self.assert_stopped_before_injection("preflight", "Component vocabulary|topology must list")
                self.assertEqual(self.server.paths(), [])

    def test_ready_model_records_runtime_before_cluster_preparation(self):
        experiment = self.experiment()
        def cluster_prepare(_self=None):
            # The model runtime is already recorded when cluster work starts.
            self.assertTrue((experiment.directory / "model-runtime.json").is_file())
        with patch.object(self.base, "prepare", side_effect=cluster_prepare) as prepared:
            experiment.prepare()
        prepared.assert_called_once()
        runtime = json.loads((experiment.directory / "model-runtime.json").read_text())
        self.assertEqual(runtime["server_version"], "0.0.0-fixture")
        self.assertEqual(runtime["keep_alive"], "30m")
        self.assertEqual(runtime["models"]["fixture-model"]["placement"]["gpu_fraction"], .75)
        self.assertEqual(runtime["models"]["fixture-model"]["quantization_level"], "Q4_K_M")
        self.assertEqual(runtime["warm_up"][0]["load_duration_seconds"], .25)
        self.assertTrue(runtime["captured_at"])
        chat = [r["body"] for r in self.server.requests if r["path"] == "/api/chat"]
        self.assertEqual([body["keep_alive"] for body in chat], ["30m"])
        self.assertNotIn("agent_failure", experiment.summary)
        ready = [c for c in experiment.event.call_args_list if c.args[:2] == (3, "status")]
        self.assertEqual(ready[0].kwargs["progress"]["stage"], "warm_up")
        experiment.inject_fault.assert_not_called()
        experiment.store.close()

    def test_agent_config_can_override_keep_alive(self):
        experiment = self.experiment(keep_alive="5m")
        with patch.object(self.base, "prepare"):
            experiment.prepare()
        self.assertEqual(json.loads((experiment.directory / "model-runtime.json").read_text())["keep_alive"], "5m")
        experiment.store.close()


if __name__ == "__main__":
    unittest.main()
