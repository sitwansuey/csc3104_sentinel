"""Agent-mode planning through the durable Planner journal, with a scripted model.

The real RCA agent, AgentPlanner, prepare(), PlannerStore, Coordinator, Guard,
Executor and Verifier run; the model and Kubernetes are scripted fakes. Every
failure must end in a saved, delivered ESCALATE (NEEDS_REVIEW), never an OPEN
incident or a fallback repair, and a saved proposal must replay without a new
model call.
"""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from agent.agent import ANALYSIS
from agent.llm import (ModelOutputInvalid, ModelUnavailable, Ollama, PlanningBudgetExceeded)
from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.guard_pipeline import process_guard_requests
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance, legacy_local_approve
from services.coordinator.core import handle
from services.guard.common import json_sha256
from services.observer.core import detect
from services.planner.agent_provider import AgentPlanner
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from services.planner.worker import PlanningError, escalation, prepare
from services.verifier.core import inspect_dependencies
from tests.support.fake_ollama import FakeOllama, Reply, chat_reply

LOG_MARKER = "RAW-LOG-MARKER-7f3a"
EXPLANATION_MARKER = "REPORTER-EXPLANATION-MARKER-91c2"


class Crash(BaseException):
    """Stands in for the planning process dying inside an attempt."""


class ScriptedClient:
    """One model client per attempt, like Ollama: it records calls and answers by role."""

    def __init__(self, script):
        self.script, self.calls = script, []

    def ask(self, role, prompt, schema):
        call = {"role": role, "call_id": len(self.calls) + 1, "status": "running", "seconds": 0.0}
        self.calls.append(call)
        self.script.asked.append(role)
        answer = self.script.answer(role)
        if isinstance(answer, BaseException):
            call["status"] = "failed"
            raise answer
        call["status"] = "completed"
        return deepcopy(answer)


class Script:
    def __init__(self, baseline):
        self.baseline = baseline
        self.asked = []
        self.overrides = {}  # role -> function(attempt) returning an answer or an exception
        self.planner = None

    def client(self, config, *, progress, deadline):
        return ScriptedClient(self)

    def answer(self, role):
        if role in self.overrides:
            return self.overrides[role](self.planner.attempt)
        return self.default(role)

    def default(self, role):
        return {
            "Analyzer": {"summary": "Startup configuration error", "complete": True,
                         "confidence": 0.9, "next_steps": ""},
            "Reporter": {"failures": [{"root cause component": "demo-api",
                                       "root cause reason": "Invalid startup configuration"}],
                         "explanation": EXPLANATION_MARKER, "limitations": "Synthetic test"},
            "Recovery Planner": {"action": "ROLLBACK", "parameters": {
                key: self.baseline[key] for key in ("baseline_id", "template_sha256")},
                "rationale": "Restore the measured healthy template for the startup failure"},
        }[role]


class AgentJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "records").mkdir()
        self.f = OfflineFlow(self.root / "records")
        self.incident = self.f.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        handle(self.incident, self.f.store, self.f.bus)
        self.path = self.root / "planner.db"
        self.journal = PlannerStore(self.path, clock=lambda: self.f.now)
        self.script = Script(self.f.baseline)
        self.reads = []  # Deployment overrides, consumed one per read
        self.exports = 0
        self.readings = []
        self.mono = [1000.0]
        self.agent = self.planner()

    def tearDown(self):
        self.journal.close()
        self.f.close()

    def planner(self, **config):
        agent = AgentPlanner({"planning_budget_seconds": 600, **config}, folder=self.root / "agent",
                             baseline=self.f.baseline, telemetry=self.export, deployment_reader=self.read,
                             llm_factory=self.script.client, clock=lambda: self.mono[0])
        self.script.planner = agent
        return agent

    def export(self, destination, incident):
        self.exports += 1
        destination.mkdir(parents=True, exist_ok=False)
        (destination / "logs.csv").write_text(
            "timestamp,cmdb_id,message\n"
            "2026-10-10T12:00:30Z,demo-api,Error invalid startup configuration " + LOG_MARKER + "\n",
            encoding="utf-8")

    def read(self, target):
        if self.reads:
            return self.reads.pop(0)
        return self.f.cluster.read(target)["deployment"]

    def restart(self):
        """A new planning process: new journal connection, new AgentPlanner, same files."""
        self.journal.close()
        self.f.restart()
        self.journal = PlannerStore(self.path, clock=lambda: self.f.now)
        self.agent = self.planner()

    def plan(self):
        return process_planner_requests(self.f.store, self.f.bus, journal=self.journal, **self.agent.options())

    def step(self):
        f = self.f

        def assess(proposal):
            f.now += timedelta(seconds=1)
            handle(inspect_dependencies(proposal, f.graph, f.cluster.read(proposal["target"]), now=f.now),
                   f.store, f.bus)

        def guard():
            process_guard_requests(f.store, f.bus, state_reader=lambda p: f.cluster.read(p["target"]),
                                   policy_reader=lambda p: f.policy, baseline_reader=lambda p: f.baseline,
                                   clock=lambda: f.now)
        return advance(f.store, self.incident["incident_id"], plan=self.plan, assess=assess, guard=guard,
                       # Offline flow: the trusted local approval, as in test_recovery_runner.
                       approve=legacy_local_approve(f.store, lambda p: "operator"), execute=lambda p: f.execute(),
                       verify=lambda p, r: f.verify(self.readings))

    def saved(self):
        return self.journal.get(self.incident["incident_id"])

    def assert_escalated(self, code, attempts, proposal=None):
        saved = self.saved()
        self.assertEqual(self.f.state(), "NEEDS_REVIEW")
        self.assertEqual((saved["error_code"], saved["attempts"], saved["delivered"]), (code, attempts, 1))
        self.assertEqual(saved["proposal"], proposal or escalation(self.incident, code))
        self.assertEqual(self.f.store.action(saved["proposal"]["action_id"])["proposal"], saved["proposal"])
        self.assertEqual(self.f.store.pending_routes("guard.request"), [])
        self.assertEqual(self.f.store.pending_routes("planner.request"), [])
        self.assertEqual(self.f.cluster.writes, [])
        return saved

    def test_success_reaches_recovered_in_one_journaled_attempt(self):
        self.assertEqual(self.step(), "VERIFYING")
        self.f.now = self.f.now.replace(minute=1, second=10)
        self.readings = fixture("health.json")["stable"]
        self.assertEqual(self.step(), "RECOVERED")
        saved = self.saved()
        self.assertEqual((saved["attempts"], saved["delivered"], saved["error_code"]), (1, 1, None))
        proposal = saved["proposal"]
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.f.store.action(proposal["action_id"])["proposal"], proposal)
        self.assertEqual(proposal["based_on_resource_version"], "102")
        self.assertEqual(len(self.f.cluster.writes), 1)
        self.assertEqual(self.script.asked, ["Analyzer", "Reporter", "Recovery Planner"])
        [attempt] = self.journal.attempts(self.incident["incident_id"])
        self.assertEqual(attempt["usage"]["model_calls"], 3)
        self.assertEqual(attempt["usage"]["planning_attempt"], 1)
        self.assertEqual(attempt["context"]["diagnosis"]["prediction"]["1"]["root cause component"], "demo-api")
        self.assertTrue((self.root / "agent/attempt-1/agent/report.json").is_file())

    def test_journal_keeps_no_workspace_path_raw_log_or_reporter_explanation(self):
        self.assertEqual(self.plan()[0]["action"], "ROLLBACK")
        recorded = json.dumps({"row": self.saved(), "attempts": self.journal.attempts(self.incident["incident_id"])})
        for marker in (LOG_MARKER, EXPLANATION_MARKER, str(self.root), self.root.name, "workspace"):
            self.assertNotIn(marker, recorded)
        context = self.journal.attempts(self.incident["incident_id"])[0]["context"]
        self.assertEqual(set(context), {"diagnosis"})
        self.assertEqual(context["diagnosis"]["tools"][0]["tool"], "logs")
        self.assertFalse(context["diagnosis"]["abstained"])

    def test_unavailable_model_retries_with_backoff_across_restart_then_escalates(self):
        self.script.overrides["Analyzer"] = lambda attempt: ModelUnavailable(
            "Ollama Analyzer failed for test-model at 127.0.0.1:9: SECRET refused")
        self.assertEqual(self.step(), "OPEN")
        self.assertEqual((self.saved()["attempts"], self.saved()["error_code"]), (1, "MODEL_UNAVAILABLE"))
        self.f.now += timedelta(seconds=4)
        self.assertEqual(self.step(), "OPEN")  # Backoff is 5 s, so no new attempt yet.
        self.assertEqual(self.saved()["attempts"], 1)
        self.f.now += timedelta(seconds=1)
        self.assertEqual(self.step(), "OPEN")
        self.assertEqual(self.saved()["attempts"], 2)
        self.restart()
        self.f.now += timedelta(seconds=9)
        self.assertEqual(self.step(), "OPEN")  # The second backoff (10 s) survived the restart.
        self.f.now += timedelta(seconds=1)
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        saved = self.assert_escalated("MODEL_UNAVAILABLE", 3, escalation(self.incident))
        self.assertNotIn("SECRET", json.dumps(saved))
        self.assertEqual(self.script.asked, ["Analyzer"] * 3)
        self.assertEqual([a["usage"]["planning_attempt"] for a in self.journal.attempts(saved["incident_id"])],
                         [1, 2, 3])
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assertEqual(self.script.asked, ["Analyzer"] * 3)

    def test_abstention_is_one_saved_escalation_without_a_recovery_planner_call(self):
        self.script.overrides["Reporter"] = lambda attempt: {"failures": [], "explanation": "Insufficient",
                                                             "limitations": "None"}
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("ABSTAINED", 1)
        self.assertNotIn("Recovery Planner", self.script.asked)
        self.assertTrue(self.journal.attempts(self.incident["incident_id"])[0]["context"]["diagnosis"]["abstained"])

    def test_diagnosis_naming_another_component_is_terminal(self):
        self.script.overrides["Reporter"] = lambda attempt: {"failures": [{
            "root cause component": "frontend calling demo-api", "root cause reason": "Timeout"}],
            "explanation": "x", "limitations": "x"}
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("DIAGNOSIS_OFF_TARGET", 1)
        self.assertNotIn("Recovery Planner", self.script.asked)

    def test_model_choosing_no_action_is_terminal(self):
        self.script.overrides["Recovery Planner"] = lambda attempt: {
            "action": "NO_ACTION", "parameters": {}, "rationale": "Nothing to do"}
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("NO_ACTION_PROPOSED", 1)

    def test_model_escalation_is_saved_as_its_own_answer(self):
        self.script.overrides["Recovery Planner"] = lambda attempt: {
            "action": "ESCALATE", "parameters": {}, "rationale": "Cause needs another action"}
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        saved = self.saved()
        self.assertEqual((saved["proposal"]["rationale"], saved["error_code"]), ("Cause needs another action", None))

    def test_invalid_output_twice_then_valid_uses_three_distinct_attempts(self):
        self.script.overrides["Analyzer"] = lambda attempt: ModelOutputInvalid(
            "Ollama Analyzer failed: Model output is not valid JSON") if attempt == 1 else self.script.default(
            "Analyzer")
        invented = {"action": "ROLLBACK", "parameters": {"baseline_id": "invented", "template_sha256": "0" * 64},
                    "rationale": "Invented baseline"}
        self.script.overrides["Recovery Planner"] = lambda attempt: invented if attempt == 2 else (
            self.script.default("Recovery Planner"))
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.saved()["error_code"], "INVALID_MODEL_OUTPUT")
        self.f.now += timedelta(seconds=5)
        self.assertEqual(self.plan(), [])
        self.assertEqual((self.saved()["attempts"], self.saved()["error_code"]), (2, "INVALID_MODEL_OUTPUT"))
        self.f.now += timedelta(seconds=10)
        proposal = self.plan()[0]
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.saved()["attempts"], 3)
        self.assertEqual(sorted(p.name for p in (self.root / "agent").iterdir()),
                         ["attempt-1", "attempt-2", "attempt-3"])
        self.assertEqual(self.exports, 3)

    def test_lost_delivery_replays_the_same_action_without_a_model_call(self):
        with patch("runtime.planner_pipeline.record_message", side_effect=OSError("lost result")):
            with self.assertRaises(OSError):
                self.plan()
        saved = self.saved()
        self.assertEqual((saved["delivered"], self.f.state()), (0, "OPEN"))
        asked = list(self.script.asked)
        self.restart()
        self.assertEqual(self.plan(), [saved["proposal"]])
        self.assertEqual(self.script.asked, asked)
        self.assertEqual(self.saved()["attempts"], 1)
        self.assertEqual(self.f.store.action(saved["proposal"]["action_id"])["proposal"], saved["proposal"])
        self.assertEqual(self.plan(), [])

    def test_process_death_mid_diagnosis_expires_lease_and_fences_the_stale_attempt(self):
        tokens = []
        claim = self.journal.claim

        def recording(*args, **kwargs):
            result = claim(*args, **kwargs)
            tokens.append(result)
            return result
        self.script.overrides["Analyzer"] = lambda attempt: Crash()
        with patch.object(self.journal, "claim", side_effect=recording), self.assertRaises(Crash):
            self.plan()
        stale = tokens[0][0]
        self.assertEqual((self.saved()["attempts"], self.saved()["proposal"]), (1, None))
        del self.script.overrides["Analyzer"]
        self.restart()
        self.assertEqual(self.plan(), [])  # The dead attempt still holds its lease.
        self.assertEqual(self.script.asked, ["Analyzer"])
        self.f.now += timedelta(seconds=self.agent.lease_seconds)
        proposal = self.plan()[0]
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.saved()["attempts"], 2)
        self.assertTrue((self.root / "agent/attempt-2/agent/report.json").is_file())
        with self.assertRaises(ValueError):
            self.journal.save(self.incident["incident_id"], stale, escalation(self.incident))
        self.assertEqual(self.saved()["proposal"], proposal)

    def test_target_changed_during_inference_is_terminal(self):
        before = self.f.cluster.read(self.incident["target"])["deployment"]
        changed = deepcopy(before)
        changed["spec"]["template"]["metadata"]["labels"]["changed"] = "during-inference"
        self.reads = [before, changed]
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("TARGET_CHANGED", 1)
        self.f.now += timedelta(hours=1)
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assertEqual(self.saved()["attempts"], 1)

    def test_recreated_target_before_diagnosis_is_terminal(self):
        recreated = deepcopy(self.f.cluster.read(self.incident["target"])["deployment"])
        recreated["metadata"]["uid"] = "another-uid"
        self.reads = [recreated]
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("TARGET_CHANGED", 1)
        self.assertEqual(self.script.asked, [])

    def test_resource_version_change_rebinds_to_a_new_deterministic_action(self):
        before = self.f.cluster.read(self.incident["target"])["deployment"]
        after = deepcopy(before)
        after["metadata"]["resourceVersion"] = "103"  # A status update during inference.
        self.reads = [before, after]
        with patch("runtime.planner_pipeline.record_message", side_effect=OSError("lost result")):
            with self.assertRaises(OSError):
                self.plan()
        saved = self.saved()["proposal"]
        self.assertEqual(saved["based_on_resource_version"], "103")
        body = {key: value for key, value in saved.items() if key != "action_id"}
        self.assertEqual(saved["action_id"], "ACT-" + json_sha256(body)[:24])
        unbound = dict(body, based_on_resource_version=self.incident["observed_resource_version"])
        self.assertNotEqual(saved["action_id"], "ACT-" + json_sha256(unbound)[:24])
        asked = len(self.script.asked)
        self.restart()
        self.assertEqual(self.plan(), [saved])  # The rebound action is the one replayed.
        self.assertEqual(len(self.script.asked), asked)
        # A separate run with the same inputs derives the same identity.
        self.reads = [before, after]
        script = Script(self.f.baseline)
        other = AgentPlanner({"planning_budget_seconds": 600}, folder=self.root / "other",
                             baseline=self.f.baseline, telemetry=self.export, deployment_reader=self.read,
                             llm_factory=script.client, clock=lambda: self.mono[0])
        script.planner = other
        with PlannerStore(self.root / "other.db", clock=lambda: self.f.now) as journal:
            self.assertEqual(prepare(self.incident, journal=journal, **other.options()), saved)

    def test_budget_exhaustion_stops_before_the_next_model_call(self):
        def slow(attempt):
            self.mono[0] += 601  # The Analyzer reply arrives after the planning budget.
            return self.script.default("Analyzer")
        self.script.overrides["Analyzer"] = slow
        self.assertEqual(self.step(), "OPEN")
        self.assertEqual((self.saved()["attempts"], self.saved()["error_code"]), (1, "BUDGET_EXCEEDED"))
        self.assertEqual(self.script.asked, ["Analyzer"])  # The Reporter call never started.
        [attempt] = self.journal.attempts(self.incident["incident_id"])
        self.assertEqual(attempt["context"], {"diagnosis": None})
        self.assertGreaterEqual(attempt["usage"]["seconds"], 600)
        self.f.now += timedelta(seconds=15)
        self.assertEqual(self.step(), "OPEN")
        self.f.now += timedelta(seconds=15)
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assert_escalated("BUDGET_EXCEEDED", 3, escalation(self.incident))

    def test_lease_must_outlive_the_planning_budget(self):
        options = dict(folder=self.root, baseline=self.f.baseline, telemetry=self.export,
                       deployment_reader=self.read)
        self.assertEqual(AgentPlanner({"planning_budget_seconds": 120}, **options).lease_seconds, 180)
        for lease in (120, 179, "600", [600]):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                AgentPlanner({"planning_budget_seconds": 120}, lease_seconds=lease, **options)
        for budget in (0, -1, float("inf"), float("nan"), "600", True, 86400):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                AgentPlanner({"planning_budget_seconds": budget}, **options)

    def assert_classified_retry(self, code):
        self.assertEqual(self.step(), "OPEN")
        self.assertEqual((self.saved()["attempts"], self.saved()["error_code"]), (1, code))
        self.assertEqual(self.script.asked, [])
        self.assertEqual(self.agent.failures[-1]["code"], code)

    def test_failed_telemetry_export_is_a_classified_retry(self):
        self.agent.telemetry = Mock(side_effect=OSError("disk full"))
        self.assert_classified_retry("TELEMETRY_UNAVAILABLE")

    def test_failed_target_read_is_a_classified_retry(self):
        self.agent.deployment_reader = Mock(side_effect=RuntimeError("kubectl failed"))
        self.assert_classified_retry("TARGET_UNREADABLE")


class PlanningHookTests(unittest.TestCase):
    """prepare() hooks and the error taxonomy, without the agent."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.f = OfflineFlow(temporary.name)
        self.addCleanup(self.f.close)
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        self.journal = PlannerStore(Path(temporary.name) / "planner.db", clock=lambda: self.f.now)
        self.addCleanup(self.journal.close)
        self.diagnosis = {"incident_id": self.incident["incident_id"],
                          "prediction": {"1": {"root cause component": "demo-api"}},
                          "report": {"explanation": "free text"}, "workspace": "/somewhere"}

    def prepare(self, **hooks):
        return prepare(self.incident, journal=self.journal, baseline_reader=lambda i: [self.f.baseline],
                       retriever=lambda query: {"kp": retrieve_runbooks(self.incident)},
                       provider=lambda context: {key: value for key, value in mock_provider(context).items()
                                                 if key != "evidence_refs"}, **hooks)

    def test_finalize_may_only_rebind_version_and_identity(self):
        def widen(proposal):
            proposal["rationale"] = "changed after validation"
            proposal["action_id"] = "ACT-" + json_sha256(
                {k: v for k, v in proposal.items() if k != "action_id"})[:24]
            return proposal
        self.assertIsNone(self.prepare(diagnoser=lambda incident, attempt: self.diagnosis, finalize=widen))
        self.assertEqual(self.journal.get(self.incident["incident_id"])["error_code"], "INVALID_MODEL_OUTPUT")

    def test_finalize_must_recompute_the_action_identity(self):
        def stale_identity(proposal):
            proposal["based_on_resource_version"] = "999"
            return proposal
        self.assertIsNone(self.prepare(diagnoser=lambda incident, attempt: self.diagnosis,
                                       finalize=stale_identity))
        self.assertEqual(self.journal.get(self.incident["incident_id"])["error_code"], "INVALID_MODEL_OUTPUT")

    def test_diagnoser_receives_the_claimed_attempt_and_a_copy(self):
        seen = []

        def diagnoser(incident, attempt):
            seen.append(attempt)
            incident["target"]["name"] = "mutated"
            return self.diagnosis
        proposal = self.prepare(diagnoser=diagnoser)
        self.assertEqual((seen, proposal["action"], proposal["target"]), ([1], "ROLLBACK", self.incident["target"]))
        context = self.journal.attempts(self.incident["incident_id"])[0]["context"]
        self.assertNotIn("/somewhere", json.dumps(context))
        self.assertNotIn("free text", json.dumps(context))

    def test_terminal_error_needs_a_fixed_rationale_and_codes_are_identifiers(self):
        with self.assertRaises(ValueError):
            PlanningError("SOMETHING_NEW", terminal=True)
        for code in ("", "lower", "X" * 65, None):
            with self.subTest(code=code), self.assertRaises(ValueError):
                PlanningError(code)
        self.assertNotEqual(escalation(self.incident, "ABSTAINED")["action_id"],
                            escalation(self.incident)["action_id"])

    def test_hooks_require_the_durable_journal(self):
        with self.assertRaisesRegex(ValueError, "durable Planner journal"):
            process_planner_requests(self.f.store, self.f.bus, baseline_reader=lambda i: [],
                                     retriever=retrieve_runbooks, provider=mock_provider,
                                     diagnoser=lambda incident, attempt: self.diagnosis)


class PlanningDeadlineTransportTests(unittest.TestCase):
    """The real Ollama client against a loopback fake: deadline, cap and error classes."""

    def setUp(self):
        self.server = FakeOllama()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)

    def client(self, deadline=None, **config):
        return Ollama({"base_url": self.server.url, "model": "fixture-model", "timeout": 5,
                       "progress_interval_seconds": 60, **config}, deadline=deadline)

    def test_no_call_starts_inside_the_last_seconds_of_the_budget(self):
        llm = self.client(deadline=time.monotonic() + 1)
        with self.assertRaisesRegex(PlanningBudgetExceeded, "planning deadline"):
            llm.ask("Analyzer", "{}", ANALYSIS)
        self.assertEqual(self.server.paths("/api/chat"), [])
        self.assertEqual(llm.calls, [])

    def test_a_slow_reply_is_cut_at_the_planning_deadline_and_not_retried(self):
        answer = {"summary": "s", "complete": True, "confidence": 1, "next_steps": ""}
        self.server.chat = Reply(chat_reply(answer), drip=.05)
        with patch("agent.llm.MIN_CALL_SECONDS", .3):
            llm = self.client(deadline=time.monotonic() + 1)
            started = time.monotonic()
            with self.assertRaises(PlanningBudgetExceeded):
                llm.ask("Analyzer", "{}", ANALYSIS)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(self.server.paths("/api/chat")), 1)

    def test_failure_classes_stay_runtime_errors(self):
        self.server.chat = Reply(chat_reply("not json"))
        with self.assertRaises(ModelOutputInvalid) as invalid:
            self.client().ask("Analyzer", "{}", ANALYSIS)
        with FakeOllama() as closed:
            url = closed.url
        with self.assertRaises(ModelUnavailable) as unavailable:
            Ollama({"base_url": url, "model": "fixture-model", "timeout": 1}).ask("Analyzer", "{}", ANALYSIS)
        self.server.chat = Reply(chat_reply({"summary": "s", "complete": True, "confidence": 1, "next_steps": ""}))
        capped = self.client(max_model_calls=1)
        capped.ask("Analyzer", "{}", ANALYSIS)
        with self.assertRaisesRegex(PlanningBudgetExceeded, "max_model_calls") as budget:
            capped.ask("Analyzer", "{}", ANALYSIS)
        for caught in (invalid, unavailable, budget):
            self.assertIsInstance(caught.exception, RuntimeError)

    def test_deadline_must_be_a_finite_monotonic_time(self):
        for deadline in (float("nan"), float("inf"), "soon", True):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                self.client(deadline=deadline)


if __name__ == "__main__":
    unittest.main()
