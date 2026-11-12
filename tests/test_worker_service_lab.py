"""Worker service additions for lab runs: the gated file cluster, the labelled deterministic
planner, test clock offsets and the /healthz and /readyz probes."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import UTC, datetime
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from experiments.distributed import fixtures
from experiments.validation.demo_offline_flow import fixture
from runtime import fault_points, worker_service
from runtime.remote_adapters import DeterministicProvider, PlannerAdapter
from runtime.worker_health import WorkerHealth, serve_health
from services.observer.core import detect
from services.planner.store import PlannerStore
from tests.support.file_cluster import FileCluster


class LabWorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.inputs = fixtures.build()
        FileCluster.create(self.directory / "cluster.db", self.inputs["snapshot"]).close()
        self.paths = {}
        for name in ("policy", "baseline", "graph"):
            self.paths[name] = self.directory / (name + ".json")
            self.paths[name].write_text(json.dumps(self.inputs[name]), encoding="utf-8")
        self.paths["allowed"] = self.directory / "allowed.json"
        self.paths["allowed"].write_text(json.dumps([self.inputs["target"]]), encoding="utf-8")
        (self.directory / "faults").mkdir()
        self.gate = {fault_points.ENV_DIRECTORY: str(self.directory / "faults"),
                     fault_points.ENV_CLUSTER: fixtures.CLUSTER_ID, "TEST_LAB_TOKEN": "t" * 40,
                     fault_points.ENV_CLOCK_OFFSET: ""}

    def config(self, role, **changes):
        cluster = "file:" + str(self.directory / "cluster.db")
        config = {"role": role, "identity": role + "-1", "url": "http://127.0.0.1:9", "token_env": "TEST_LAB_TOKEN",
                  "cluster": cluster, "context": "file-cluster", "cluster_id": fixtures.CLUSTER_ID,
                  "kubeconfig": str(self.directory / "cluster.db"), "policy": str(self.paths["policy"]),
                  "baseline": str(self.paths["baseline"]), "allowed_targets": str(self.paths["allowed"]),
                  "dependency_graph": str(self.paths["graph"]), "baselines": str(self.paths["baseline"]),
                  "provider": "deterministic",
                  "approvals": {"url": "http://127.0.0.1:9", "identity": role + "-1", "token_env": "TEST_LAB_TOKEN"}}
        config.update(changes)
        return config

    def adapter(self, config, **environment):
        with patch.dict(os.environ, {**self.gate, **environment}):
            state = self.directory / config["role"]
            state.mkdir(exist_ok=True)
            return worker_service.adapter(config, state, self.stack)

    def test_a_file_cluster_is_refused_outside_a_lab_fault_run(self):
        for environment in ({fault_points.ENV_DIRECTORY: ""}, {fault_points.ENV_CLUSTER: "sentinel-lab"}):
            with self.subTest(environment=environment), self.assertRaises(ValueError):
                self.adapter(self.config("verifier"), **environment)
        with self.assertRaises(ValueError):
            self.adapter(self.config("verifier", cluster="kind:anything"))
        self.assertIsNone(worker_service.lab_cluster({"role": "planner"}, self.stack))

    def test_lab_executor_uses_the_production_adapter_with_stop_points_and_the_test_clock(self):
        with patch("runtime.approval_http.ApprovalClient"):
            executor = self.adapter(self.config("executor"), **{fault_points.ENV_CLOCK_OFFSET: "-300"})
        adapters = executor.adapters
        self.assertIsInstance(adapters.ledger, fault_points.CheckpointLedger)
        self.assertIsInstance(adapters.cluster, fault_points.CheckpointCluster)
        self.assertEqual(adapters.cluster.read(self.inputs["target"])["deployment"]["metadata"]["resourceVersion"],
                         "102")
        skew = (datetime.now(UTC) - adapters.clock()).total_seconds()
        self.assertAlmostEqual(skew, 300, delta=5)

    def test_lab_guard_and_verifier_read_the_file_cluster(self):
        with patch("runtime.approval_http.ApprovalClient"):
            guard = self.adapter(self.config("guard"))
        snapshot = guard.state_reader({"target": self.inputs["target"]})
        self.assertEqual(snapshot["cluster_id"], fixtures.CLUSTER_ID)
        verifier = self.adapter(self.config("verifier", samples_per_pass=1))
        (reading,) = verifier.sample_reader({"target": self.inputs["target"]})
        self.assertEqual(reading["http_status"], 503)  # The failed template is still live.

    def test_a_real_cluster_config_never_reaches_the_file_cluster(self):
        config = self.config("verifier")
        del config["cluster"]
        with patch("services.observer.kubernetes.KubectlReader"), \
                patch("services.verifier.live.LiveEvidenceCollector") as collector:
            self.adapter(dict(config, evidence=str(self.paths["policy"])))
        collector.assert_called_once()


class DeterministicPlannerTests(unittest.TestCase):
    def test_every_call_is_labelled_synthetic_and_reaches_the_stop_point(self):
        stops = []
        provider = DeterministicProvider(checkpoint=lambda: stops.append("planner.mid_inference"))
        provider.usage["synthetic_planner"] = False  # A caller cannot strip the label from the next call.
        incident = detect(fixture("observations.json")["failed-rollout"], fixture("policy.json"))
        context = {"incident": incident, "baselines": [fixture("baseline.json")]}
        proposal = provider(context)
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(provider.usage, {"provider": "deterministic", "synthetic_planner": True,
                                          "model": None, "calls": []})
        self.assertEqual(stops, ["planner.mid_inference"])

    def test_the_planner_journal_stores_the_synthetic_label_with_the_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            incident = detect(fixture("observations.json")["failed-rollout"], fixture("policy.json"))
            with PlannerStore(Path(directory) / "planner.db") as journal:
                planner = PlannerAdapter(journal, baseline_reader=lambda _: [fixture("baseline.json")],
                                         provider=DeterministicProvider())
                proposal, _ = planner.process({"topic": "planner.request", "payload": {"incident": incident}}, None)
                (attempt,) = journal.attempts(incident["incident_id"])
        self.assertEqual(proposal["kind"], "proposal")
        self.assertIs(attempt["usage"]["synthetic_planner"], True)

    def test_provider_selection_is_explicit_and_bounded(self):
        provider, lease = worker_service.planner_provider({"provider": "deterministic", "attempt_lease_seconds": 5})
        self.assertIsInstance(provider, DeterministicProvider)
        self.assertEqual(lease, 5)
        for config in ({"provider": "Deterministic"}, {"provider": "mock"},
                       {"provider": "deterministic", "attempt_lease_seconds": 0},
                       {"provider": "deterministic", "attempt_lease_seconds": True}):
            with self.subTest(config=config), self.assertRaises(ValueError):
                worker_service.planner_provider(config)
        with tempfile.TemporaryDirectory() as directory:
            model = Path(directory) / "model.json"
            model.write_text(json.dumps({"model": "m", "timeout": 10}), encoding="utf-8")
            with patch("runtime.worker_service.OllamaProvider") as ollama:
                self.assertEqual(worker_service.planner_provider({"model_config": str(model)})[1], 50)
            ollama.assert_called_once()


class HealthTests(unittest.TestCase):
    def test_liveness_and_readiness_follow_the_loop_and_coordinator_contact(self):
        now = [100.0]
        health = WorkerHealth(ready_seconds=30, clock=lambda: now[0])
        stopping = threading.Event()
        self.assertEqual(health.live(stopping)[0], 503)
        self.assertEqual(health.ready(stopping), (503, {"ready": False, "contact_age_seconds": None}))
        health.heartbeat()
        health.contacted()
        self.assertEqual(health.ready(stopping)[0], 503)  # Configuration not yet accepted.
        health.configured()
        self.assertEqual(health.ready(stopping)[0], 200)
        now[0] += 31
        self.assertEqual(health.ready(stopping), (503, {"ready": False, "contact_age_seconds": 31.0}))
        self.assertEqual(health.live(stopping)[0], 200)
        stopping.set()
        self.assertEqual(health.live(stopping)[0], 503)

    def test_probes_are_served_on_loopback_without_configuration_details(self):
        health = WorkerHealth()
        with serve_health(health, 0) as server:
            port = server.server_address[1]
            self.assertEqual(server.server_address[0], "127.0.0.1")

            def get(path):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    connection.request("GET", path)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
            self.assertEqual(get("/readyz")[0], 503)
            health.heartbeat()
            health.configured()
            health.contacted()
            status, body = get("/healthz")
            self.assertEqual((status, body["alive"], set(body)), (200, True, {"alive", "loop_age_seconds"}))
            self.assertEqual(get("/readyz")[0], 200)
            self.assertEqual(set(get("/readyz")[1]), {"ready", "contact_age_seconds"})
            self.assertEqual(get("/other")[0], 404)

    def test_entrypoint_validates_probe_options_and_serves_while_polling(self):
        for arguments in (["--health-port", "70000"], ["--ready-seconds", "0"], ["--ready-seconds", "nan"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                worker_service.main(["--config", "unused.json", *arguments])
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "planner.json"
            config.write_text(json.dumps({"role": "planner", "url": "http://127.0.0.1:9", "identity": "planner-1",
                                          "token_env": "TEST_LAB_TOKEN", "state_directory": str(Path(directory) / "s"),
                                          "provider": "deterministic", "baselines": str(config)}), encoding="utf-8")
            output = io.StringIO()
            with patch.dict(os.environ, {"TEST_LAB_TOKEN": "t" * 40}), redirect_stdout(output), \
                    redirect_stderr(io.StringIO()), patch("runtime.worker_service.serve_health",
                                                          wraps=serve_health) as served:
                self.assertEqual(worker_service.main(["--config", str(config), "--once", "--health-port", "0"]), 1)
            served.assert_called_once()
            self.assertEqual(json.loads(output.getvalue().splitlines()[-1])["status"], "retry")


if __name__ == "__main__":
    unittest.main()
