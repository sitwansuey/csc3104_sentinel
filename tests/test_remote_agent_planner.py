"""The remote agent Planner worker: runtime.worker_service with role planner and mode agent.

The real AgentPlanner, prepare(), PlannerStore and worker loop run. The model is
a scripted client or tests/support/fake_ollama.py on loopback, and Kubernetes is
a fake or the file test cluster. One test runs the worker as its own OS process
against a loopback Coordinator, so the journal is used on a separate process
path; that is still one host, not a container or a second machine.
"""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import fixtures
from runtime import fault_points, worker_service
from runtime.coordinator_server import servers
from runtime.remote_adapters import AgentPlannerAdapter, DeterministicProvider, PlannerAdapter
from runtime.remote_worker import RemoteIngress
from runtime.worker_health import PreflightFailed, WorkerHealth
from runtime.worker_http import WorkerClient
from services.observer.core import detect
from services.planner.store import PlannerStore
from services.planner.worker import escalation
from tests.support.fake_ollama import MODEL, FakeOllama, Reply, chat_reply
from tests.support.file_cluster import FileCluster

ROOT = Path(__file__).resolve().parents[1]
TOKEN_ENV = "TEST_AGENT_PLANNER_TOKEN"
ROLE_PREFIX, ROLE_SUFFIX = "You are the ", " in a root cause analysis system"


def answers(baseline):
    """A correct diagnosis and a ROLLBACK to the trusted baseline, by model role."""
    return {
        "Analyzer": {"summary": "Startup configuration error after the rollout", "complete": True,
                     "confidence": 0.9, "next_steps": ""},
        "Reporter": {"failures": [{"root cause component": baseline["target"]["name"],
                                   "root cause reason": "Invalid startup configuration"}],
                     "explanation": "Container logs show the startup failure", "limitations": "Synthetic test"},
        "Recovery Planner": {"action": "ROLLBACK", "parameters": {
            key: baseline[key] for key in ("baseline_id", "template_sha256")},
            "rationale": "Restore the measured healthy template for the startup failure"},
    }


class ScriptedModel:
    """One client per attempt, like Ollama; answers by role, or raises what the test scripts."""

    def __init__(self, baseline):
        self.answers, self.asked, self.failures, self.clients = answers(baseline), [], {}, 0
        self.before = {}  # role -> a callable run just before that role answers

    def factory(self, config, *, progress, deadline):
        self.clients += 1
        model = self

        class Client:
            calls = []

            def ask(self, role, prompt, schema):
                model.asked.append(role)
                if role in model.before:
                    model.before[role]()
                failure = model.failures.get(role)
                if failure is not None:
                    self.calls.append({"role": role, "status": "failed", "seconds": 0.0})
                    raise failure
                self.calls.append({"role": role, "status": "completed", "seconds": 0.0})
                return deepcopy(model.answers[role])
        return Client()


class Inputs:
    """A file-mode run's synthetic inputs, written where a worker configuration can name them."""

    def __init__(self, root):
        self.root = Path(root)
        self.data = fixtures.build()
        self.baseline = self.data["baseline"]
        self.incident = detect(self.data["observations"], self.data["detection_policy"])
        self.telemetry = self.root / "observer"
        self.telemetry.mkdir()
        for index, observation in enumerate(self.data["observations"]):
            (self.telemetry / f"{index:03d}-observation.json").write_text(json.dumps(observation), encoding="utf-8")
        self.baseline_path = self.root / "baseline.json"
        self.baseline_path.write_text(json.dumps(self.baseline), encoding="utf-8")
        self.cluster_path = self.root / "cluster.db"
        FileCluster.create(self.cluster_path, self.data["snapshot"]).close()
        self.faults = self.root / "faults"
        self.faults.mkdir()
        self.gate = {fault_points.ENV_DIRECTORY: str(self.faults), fault_points.ENV_CLUSTER: fixtures.CLUSTER_ID}

    def model_config(self, base_url, **changes):
        path = self.root / "model.json"
        path.write_text(json.dumps({"base_url": base_url, "model": MODEL, "timeout": 10,
                                    "planning_budget_seconds": 120, "progress_interval_seconds": 60,
                                    **changes}), encoding="utf-8")
        return path

    def config(self, url, model_config, **changes):
        config = {"role": "planner", "mode": "agent", "identity": "planner-1", "url": url, "token_env": TOKEN_ENV,
                  "state_directory": str(self.root / "planner-state"), "baselines": str(self.baseline_path),
                  "model_config": str(model_config), "telemetry_directory": str(self.telemetry),
                  "cluster": "file:" + str(self.cluster_path), "context": "file-cluster",
                  "cluster_id": fixtures.CLUSTER_ID, "max_attempts": 3, "retry_seconds": 1}
        config.update(changes)
        path = self.root / "planner.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return config, path


class AgentAdapterTests(unittest.TestCase):
    """adapter('planner', mode 'agent') wires the agent path; fakes only, no network."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.inputs = Inputs(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.model_path = self.inputs.model_config("http://127.0.0.1:9")
        self.config, _ = self.inputs.config("http://127.0.0.1:9", self.model_path)

    def adapter(self, config=None, **environment):
        state = self.inputs.root / "state"
        state.mkdir(exist_ok=True)
        with patch.dict(os.environ, {**self.inputs.gate, **environment}):
            return worker_service.adapter(config or self.config, state, self.stack)

    def delivery(self):
        return {"topic": "planner.request", "payload": {"incident": deepcopy(self.inputs.incident)}}

    def test_agent_mode_passes_the_agent_hooks_and_budget_lease_to_prepare(self):
        planner = self.adapter()
        self.assertIsInstance(planner, AgentPlannerAdapter)
        self.assertIsInstance(planner.journal, PlannerStore)
        with patch("runtime.remote_adapters.prepare", return_value=None) as prepare:
            self.assertIsNone(planner.process(self.delivery(), None))
        options = prepare.call_args.kwargs
        agent = planner.last
        self.assertIs(options["journal"], planner.journal)
        self.assertEqual(options["diagnoser"], agent.diagnose)
        self.assertEqual(options["finalize"], agent.rebind)
        self.assertIs(options["provider"], agent)
        self.assertEqual(options["lease_seconds"], 120 + 60)  # planning_budget_seconds plus the margin
        self.assertEqual((options["max_attempts"], options["retry_seconds"]), (3, 1))
        self.assertTrue(agent.folder.name.startswith("incident-"))
        self.assertNotIn(self.inputs.incident["incident_id"], str(agent.folder))
        self.assertEqual(agent.config["base_url"], "http://127.0.0.1:9")
        self.assertIs(agent.config["allow_abstention"], True)

    def scripted(self, model, **options):
        self.cluster = self.stack.enter_context(FileCluster(str(self.inputs.cluster_path)))
        journal = self.stack.enter_context(PlannerStore(self.inputs.root / "planner.db", clock=lambda: self.now))
        return AgentPlannerAdapter(
            journal, config=worker_service.agent_model_config(self.config), folder=self.inputs.root / "agent",
            baseline=self.inputs.baseline, telemetry=worker_service.observer_telemetry(self.inputs.telemetry),
            deployment_reader=worker_service.agent_target_reader(self.config, self.cluster),
            llm_factory=model.factory, model_check=lambda: None, retry_seconds=1, **options)

    @property
    def now(self):
        return self._now

    def test_a_successful_attempt_saves_a_rebound_rollback_and_replays_it(self):
        from datetime import UTC, datetime, timedelta
        self._now = datetime.now(UTC)
        model = ScriptedModel(self.inputs.baseline)
        planner = self.scripted(model)
        proposal, report = planner.process(self.delivery(), None)
        self.assertIsNone(report)
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(proposal["based_on_resource_version"],
                         self.inputs.data["snapshot"]["deployment"]["metadata"]["resourceVersion"])
        self.assertEqual(model.asked, ["Analyzer", "Reporter", "Recovery Planner"])
        saved = planner.journal.get(self.inputs.incident["incident_id"])
        self.assertEqual((saved["attempts"], saved["error_code"], saved["proposal"]), (1, None, proposal))
        (attempt,) = planner.journal.attempts(self.inputs.incident["incident_id"])
        self.assertEqual(attempt["usage"]["model_calls"], 3)
        self.assertNotIn("synthetic_planner", attempt["usage"])
        self.assertTrue((planner.last.folder / "attempt-1/telemetry/logs.csv").is_file())
        self._now += timedelta(seconds=5)
        self.assertEqual(planner.process(self.delivery(), None)[0], proposal)  # Replayed, no model call.
        self.assertEqual(model.asked, ["Analyzer", "Reporter", "Recovery Planner"])

    def test_an_unavailable_model_retries_then_saves_the_escalation(self):
        from datetime import UTC, datetime, timedelta
        from agent.llm import ModelUnavailable
        self._now = datetime.now(UTC)
        model = ScriptedModel(self.inputs.baseline)
        model.failures["Analyzer"] = ModelUnavailable("connection refused")
        planner = self.scripted(model)
        for attempt in (1, 2):
            self.assertIsNone(planner.process(self.delivery(), None))
            saved = planner.journal.get(self.inputs.incident["incident_id"])
            self.assertEqual((saved["attempts"], saved["error_code"]), (attempt, "MODEL_UNAVAILABLE"))
            self._now += timedelta(seconds=10)
        proposal, _ = planner.process(self.delivery(), None)
        self.assertEqual(proposal, escalation(self.inputs.incident))
        self.assertEqual(planner.journal.get(self.inputs.incident["incident_id"])["attempts"], 3)
        self.assertNotIn("Recovery Planner", model.asked)

    def test_a_target_changed_during_inference_is_a_terminal_escalation(self):
        from datetime import UTC, datetime
        self._now = datetime.now(UTC)
        model = ScriptedModel(self.inputs.baseline)
        planner = self.scripted(model)

        def change(deployment):
            deployment["spec"]["template"]["metadata"].setdefault("labels", {})["changed"] = "yes"
        model.before["Recovery Planner"] = lambda: self.cluster.external_write(change)
        proposal, _ = planner.process(self.delivery(), None)
        self.assertEqual(proposal, escalation(self.inputs.incident, "TARGET_CHANGED"))
        self.assertEqual(planner.journal.get(self.inputs.incident["incident_id"])["attempts"], 1)

    def test_agent_mode_refuses_a_provider_several_baselines_and_another_cluster(self):
        with self.assertRaises(ValueError):  # No mixing of the synthetic provider into agent mode.
            self.adapter(dict(self.config, provider="deterministic"))
        with self.assertRaises(ValueError):
            self.adapter(dict(self.config, mode="Agent"))
        two = self.inputs.root / "two.json"
        two.write_text(json.dumps([self.inputs.baseline, self.inputs.baseline]), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.adapter(dict(self.config, baselines=str(two)))
        with self.assertRaises(ValueError):
            self.adapter(dict(self.config, telemetry_directory=str(self.inputs.root / "missing")))
        budget = self.inputs.model_config("http://127.0.0.1:9", planning_budget_seconds=0)
        with self.assertRaises(ValueError):
            self.adapter(dict(self.config, model_config=str(budget)))
        remote = self.inputs.model_config("http://203.0.113.5:11434")
        with self.assertRaises(ValueError):  # Plain http off loopback needs the explicit opt-in.
            self.adapter(dict(self.config, model_config=str(remote)))
        reader = worker_service.agent_target_reader({"cluster_id": "lab", "context": "ctx"}, None)
        with self.assertRaisesRegex(ValueError, "another cluster"):
            reader(dict(self.inputs.incident["target"], cluster_id="other"))
        with self.assertRaises(ValueError):
            worker_service.agent_target_reader({"cluster_id": "lab"}, None)

    def test_the_in_cluster_reader_reads_only_a_valid_deployment_path(self):
        with patch("services.observer.kubernetes.InClusterReader") as api:
            api.return_value.get.return_value = {"metadata": {"uid": "u"}}
            reader = worker_service.agent_target_reader({"cluster_id": "lab", "in_cluster": True}, None)
            target = {"cluster_id": "lab", "kind": "Deployment", "namespace": "demo", "name": "web"}
            self.assertEqual(reader(target), {"metadata": {"uid": "u"}})
            api.return_value.get.assert_called_once_with("/apis/apps/v1/namespaces/demo/deployments/web")
            for bad in ({"name": "../secrets"}, {"namespace": "Demo"}, {"kind": "Pod"}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    reader({**target, **bad})

    def test_preflight_fails_closed_and_reuses_only_a_passed_check(self):
        now = [100.0]
        outcomes = []

        def check():
            outcomes.append(now[0])
            if failing[0]:
                raise RuntimeError("Ollama preflight failed")
        failing = [True]
        planner = AgentPlannerAdapter(
            PlannerStore(self.inputs.root / "p.db"), config=worker_service.agent_model_config(self.config),
            folder=self.inputs.root / "agent", baseline=self.inputs.baseline, telemetry=lambda *_: None,
            deployment_reader=lambda _: None, model_check=check, preflight_seconds=30, clock=lambda: now[0])
        self.addCleanup(planner.journal.close)
        with self.assertRaises(PreflightFailed) as caught:
            planner.preflight()
        self.assertEqual(caught.exception.code, "MODEL_UNAVAILABLE")
        with self.assertRaises(PreflightFailed):  # A failure is never cached.
            planner.preflight()
        failing[0] = False
        planner.preflight()
        now[0] += 29
        planner.preflight()
        self.assertEqual(len(outcomes), 3)
        now[0] += 2
        failing[0] = True
        with self.assertRaises(PreflightFailed):
            planner.preflight()
        for seconds in (0, -1, 3601, float("nan"), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                AgentPlannerAdapter(planner.journal, config=worker_service.agent_model_config(self.config),
                                    folder=self.inputs.root, baseline=self.inputs.baseline, telemetry=None,
                                    deployment_reader=None, preflight_seconds=seconds)

    def test_the_default_preflight_asks_the_model_endpoint_for_the_configured_model(self):
        with FakeOllama() as server:
            config = worker_service.agent_model_config(
                dict(self.config, model_config=str(self.inputs.model_config(server.url))))
            check = AgentPlannerAdapter._ollama_check(config)
            check()
            self.assertEqual(server.paths(), ["/api/tags"])
            missing = AgentPlannerAdapter._ollama_check(dict(config, model="absent-model"))
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                missing()


class SecretConfigurationTests(unittest.TestCase):
    def test_a_configuration_holding_a_secret_is_refused(self):
        for config in ({"token": "x" * 40}, {"approvals": {"api_key": "k"}}, {"Authorization": "Bearer x"},
                       {"items": [{"model_password": "p"}]}, {"client-secret": "s"}, {"credentials": {}}):
            with self.subTest(config=config), self.assertRaisesRegex(ValueError, "environment variable"):
                worker_service.refuse_inline_secrets(config)
        worker_service.refuse_inline_secrets({"token_env": "X", "approvals": {"token_env": "Y"}, "kubeconfig": "k",
                                              "max_attempts": 3, "num_predict": 2048, "keep_alive": "30m"})

    def test_the_worker_refuses_a_token_in_its_or_the_model_configuration(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            inputs = Inputs(directory)
            model = inputs.model_config("http://127.0.0.1:9")
            config, _ = inputs.config("http://127.0.0.1:9", model)
            with patch.dict(os.environ, inputs.gate), self.assertRaisesRegex(ValueError, "token"):
                worker_service.adapter(dict(config, token="t" * 40), inputs.root, stack)
            secret = inputs.root / "secret-model.json"
            secret.write_text(json.dumps({"base_url": "http://127.0.0.1:9", "api_key": "k" * 40}), encoding="utf-8")
            with patch.dict(os.environ, inputs.gate), self.assertRaisesRegex(ValueError, "api_key"):
                worker_service.adapter(dict(config, model_config=str(secret)), inputs.root, stack)
            with self.assertRaisesRegex(ValueError, "api_key"):  # The provider-mode model file too.
                worker_service.planner_provider({"model_config": str(secret)})
            _, path = inputs.config("http://127.0.0.1:9", model, approvals={"token": "a" * 40})
            output = io.StringIO()
            with patch.dict(os.environ, {TOKEN_ENV: "p" * 40, **inputs.gate}), redirect_stdout(output), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(worker_service.main(["--config", str(path), "--once"]), 1)
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1]),
                             {"status": "configuration_error", "error_code": "ValueError"})
            self.assertNotIn("a" * 40, output.getvalue())


class DeterministicLabelTests(unittest.TestCase):
    def test_the_deterministic_proposal_itself_says_it_is_synthetic(self):
        with tempfile.TemporaryDirectory() as directory:
            inputs = Inputs(directory)
            with PlannerStore(inputs.root / "planner.db") as journal:
                planner = PlannerAdapter(journal, baseline_reader=lambda _: [inputs.baseline],
                                         provider=DeterministicProvider())
                proposal, _ = planner.process({"topic": "planner.request",
                                               "payload": {"incident": inputs.incident}}, None)
                (attempt,) = journal.attempts(inputs.incident["incident_id"])
        self.assertTrue(proposal["rationale"].startswith(DeterministicProvider.RATIONALE_LABEL))
        self.assertIn("not model output", proposal["rationale"])
        self.assertIs(attempt["usage"]["synthetic_planner"], True)


class AgentWorkerProcessTests(unittest.TestCase):
    """The agent Planner as its own OS process against a loopback Coordinator and a fake model server."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.inputs = Inputs(self.stack.enter_context(tempfile.TemporaryDirectory()))
        services = {"planner-1": ("planner", "p" * 40), "observer-1": ("observer", "o" * 40)}
        self.workers, _, _, _ = self.stack.enter_context(servers(
            self.inputs.root / "coordinator.db", services, {}, {}, port=0, approval_port=0, lease_seconds=30))
        self.url = f"http://127.0.0.1:{self.workers.server_port}"
        self.model = self.stack.enter_context(FakeOllama())
        self.asked = []
        replies = answers(self.inputs.baseline)

        def chat(body):
            system = body["messages"][0]["content"]
            role = system[len(ROLE_PREFIX):system.index(ROLE_SUFFIX)]
            self.asked.append(role)
            return Reply(chat_reply(replies[role]))
        self.model.chat = chat

    def client(self, identity, token):
        return WorkerClient(self.url, identity, token, timeout=5)

    def run_worker(self, path, *arguments):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("SENTINEL_")}
        environment.update({TOKEN_ENV: "p" * 40, "PYTHONIOENCODING": "utf-8", **self.inputs.gate})
        return subprocess.run([sys.executable, "-m", "runtime.worker_service", "--role", "planner",
                               "--config", str(path), "--once", *arguments], cwd=ROOT, env=environment,
                              capture_output=True, text=True, encoding="utf-8", timeout=180, check=False)

    def incident_state(self):
        from services.coordinator.core import SQLiteStore
        with SQLiteStore(self.inputs.root / "coordinator.db") as store:
            current = store.incident(self.inputs.incident["incident_id"])
            action = store.action(current["active_action_id"]) if current["active_action_id"] else None
        return current["state"], action["proposal"] if action else None

    def test_a_separate_planner_process_plans_through_its_own_journal_and_replays(self):
        RemoteIngress(self.client("observer-1", "o" * 40))(self.inputs.incident)
        _, path = self.inputs.config(self.url, self.inputs.model_config(self.model.url))
        first = self.run_worker(path)
        self.assertEqual(first.returncode, 0, first.stderr[-2000:])
        self.assertEqual(json.loads(first.stdout.splitlines()[-1])["status"], "accepted")
        state, proposal = self.incident_state()
        self.assertEqual(state, "VALIDATING")
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.asked, ["Analyzer", "Reporter", "Recovery Planner"])
        self.assertEqual(self.model.paths()[0], "/api/tags")  # The preflight ran before the claim.
        with PlannerStore(self.inputs.root / "planner-state/planner.db") as journal:
            saved = journal.get(self.inputs.incident["incident_id"])
        self.assertEqual((saved["attempts"], saved["delivered"], saved["error_code"]), (1, 1, None))
        self.assertEqual(saved["proposal"], proposal)
        second = self.run_worker(path)
        self.assertEqual(second.returncode, 0, second.stderr[-2000:])
        self.assertEqual(json.loads(second.stdout.splitlines()[-1])["status"], "idle")
        self.assertEqual(len(self.asked), 3)  # No second model call.
        for output in (first.stdout, first.stderr, second.stdout, second.stderr):
            self.assertNotIn("p" * 40, output)

    def test_a_failed_preflight_keeps_the_worker_not_ready_and_claims_nothing(self):
        RemoteIngress(self.client("observer-1", "o" * 40))(self.inputs.incident)
        with FakeOllama() as closed:
            unreachable = closed.url
        config, path = self.inputs.config(self.url, self.inputs.model_config(unreachable))
        health = WorkerHealth()
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {TOKEN_ENV: "p" * 40, **self.inputs.gate}), redirect_stdout(output), \
                redirect_stderr(errors), patch("runtime.worker_service.WorkerHealth", return_value=health):
            self.assertEqual(worker_service.main(["--config", str(path), "--once"]), 1)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1]),
                         {"status": "not_ready", "error_code": "MODEL_UNAVAILABLE"})
        health.heartbeat()
        health.contacted()
        self.assertEqual(health.ready()[0], 503)
        self.assertEqual(self.incident_state(), ("OPEN", None))
        self.assertFalse((Path(config["state_directory"]) / "agent").exists())
        # The request was never claimed: it is still there for a ready worker.
        delivery = self.client("planner-1", "p" * 40).post("claim", {})["delivery"]
        self.assertEqual(delivery["topic"], "planner.request")
        status = [json.loads(line) for line in errors.getvalue().splitlines() if '"worker.status"' in line]
        self.assertEqual([(s["status"], s["reason"], s["cause"], s["error_type"]) for s in status],
                         [("not_ready", "MODEL_UNAVAILABLE", "RuntimeError", "PreflightFailed")])

    def test_a_configuration_for_another_role_is_refused(self):
        _, path = self.inputs.config(self.url, self.inputs.model_config(self.model.url))
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(worker_service.main(["--role", "executor", "--config", str(path), "--once"]), 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "configuration_error")
        self.assertEqual(self.model.paths(), [])


class ReadinessTests(unittest.TestCase):
    def test_a_failed_prerequisite_keeps_readyz_at_503_until_a_later_check_passes(self):
        now = [10.0]
        health = WorkerHealth(clock=lambda: now[0])
        health.configured()
        health.heartbeat()
        health.contacted()
        self.assertEqual(health.ready()[0], 200)
        health.prerequisites(False)
        self.assertEqual(health.ready(), (503, {"ready": False, "contact_age_seconds": 0.0}))
        health.prerequisites(True)
        self.assertEqual(health.ready()[0], 200)


if __name__ == "__main__":
    unittest.main()
