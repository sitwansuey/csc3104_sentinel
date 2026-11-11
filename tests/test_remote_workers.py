"""Actual component algorithms over loopback HTTP, using fake Kubernetes only."""
from contextlib import ExitStack
from contextlib import redirect_stdout
from datetime import timedelta
import json
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.approval_http import ApprovalClient
from runtime.coordinator_server import servers
from runtime.remote_adapters import ExecutorAdapter, GuardAdapter, PlannerAdapter, VerifierAdapter
from runtime.remote_worker import RemoteIngress, RemoteWorker, WorkerJournal
from runtime.worker_http import WorkerClient
from runtime.worker_transport import ProtocolError, digest
from services.observer.core import detect
from services.planner.core import mock_provider
from services.planner.store import PlannerStore
from services.verifier.core import inspect_dependencies


class RemoteWorkerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.f = OfflineFlow(directory)
        self.stack.callback(self.f.close)
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        self.services = {role: (role, role[0] * 40) for role in ("observer", "planner", "guard", "executor", "verifier")}
        self.services["replacement"] = ("planner", "r" * 40)
        self.approval_services = {role: (role, "a" + role[0] * 40) for role in ("operator", "guard", "executor")}
        self.permissions = {role: [{"target": self.incident["target"], "operations": (
            ["create", "read", "revoke", "history"] if role == "operator" else ["read"])}]
            for role in self.approval_services}
        self.workers, self.approvals, self.stopping, _ = self.stack.enter_context(servers(
            self.f.path / "coordinator.db", self.services, self.approval_services, self.permissions,
            port=0, approval_port=0, clock=lambda: self.f.now, lease_seconds=2))
        self.url = f"http://127.0.0.1:{self.workers.server_port}"
        self.approval_url = f"http://127.0.0.1:{self.approvals.server_port}"
        self.provider = Mock(side_effect=mock_provider)
        self.planning = PlannerStore(self.f.path / "planner.db", clock=lambda: self.f.now)
        self.stack.callback(self.planning.close)
        self.planner = PlannerAdapter(self.planning, baseline_reader=lambda _: [self.f.baseline], provider=self.provider)
        self.guard = GuardAdapter(state_reader=lambda p: self.f.cluster.read(p["target"]),
            policy_reader=lambda _: self.f.policy, baseline_reader=lambda _: self.f.baseline,
            dependency_reader=lambda p: inspect_dependencies(p, self.f.graph, self.f.cluster.read(p["target"]), now=self.f.now),
            approval_reader=self.approval_client("guard"), clock=lambda: self.f.now)
        self.f.authorizer.approval_reader = self.approval_client("executor")
        self.verifier = VerifierAdapter(self.f.verification, policy_reader=lambda _: self.f.policy,
            sample_reader=lambda _: fixture("health.json")["stable"], clock=lambda: self.f.now,
            dependency_reader=lambda p: inspect_dependencies(p, self.f.graph, self.f.cluster.read(p["target"]), now=self.f.now))

    def client(self, role):
        return WorkerClient(self.url, role, self.services[role][1], timeout=2)

    def approval_client(self, role):
        return ApprovalClient(self.approval_url, role, self.approval_services[role][1], timeout=2)

    def worker(self, role, adapter, *, client=None):
        journal = WorkerJournal(self.f.path / (role + "-transport.db"))
        self.stack.callback(journal.close)
        return RemoteWorker(client or self.client(role), journal, adapter, renewal_interval=0.1)

    def state(self):
        return self.f.store.incident(self.incident["incident_id"])["state"]

    def plan(self):
        RemoteIngress(self.client("observer"))(self.incident)
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        action_id = self.f.store.incident(self.incident["incident_id"])["active_action_id"]
        self.proposal = self.f.store.action(action_id)["proposal"]
        finding = inspect_dependencies(self.proposal, self.f.graph, self.f.cluster.read(self.proposal["target"]), now=self.f.now)
        RemoteIngress(self.client("verifier"))(finding)

    def authorize(self):
        self.plan()
        guard = self.worker("guard", self.guard)
        self.assertEqual(guard.once()["status"], "accepted")
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        self.assertEqual(self.f.cluster.writes, [])
        self.f.now += timedelta(seconds=1)
        self.created = self.approval_client("operator").post("create", {
            "action_id": self.proposal["action_id"], "proposal_sha256": digest(self.proposal),
            "policy_version": self.f.policy["policy_version"],
            "expires_at": (self.f.now + timedelta(seconds=120)).isoformat()})
        self.f.now += timedelta(seconds=1)
        finding = inspect_dependencies(self.proposal, self.f.graph, self.f.cluster.read(self.proposal["target"]), now=self.f.now)
        RemoteIngress(self.client("verifier"))(finding)
        self.assertEqual(guard.once()["status"], "accepted")
        self.assertEqual(self.state(), "READY")

    def test_all_six_components_recover_over_http_with_managed_approval_and_owned_stores(self):
        self.authorize()
        executor = self.worker("executor", ExecutorAdapter(self.f.adapters))
        self.assertEqual(executor.once()["status"], "accepted")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.f.cluster.writes), 1)
        self.f.now = self.f.now.replace(minute=1, second=10)
        verifier = self.worker("verifier", self.verifier)
        self.assertEqual(verifier.once()["status"], "accepted")
        self.assertEqual(self.state(), "RECOVERED")
        self.assertEqual(executor.once()["status"], "idle")
        self.assertEqual(verifier.once()["status"], "idle")
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(len(self.f.cluster.writes), 1)
        for journal in (executor.journal, verifier.journal, self.planning, self.f.ledger, self.f.verification):
            self.assertNotEqual(Path(journal.db.execute("PRAGMA database_list").fetchone()[2]), self.f.path / "coordinator.db")

    def test_unchanged_uncertainty_is_retried_with_growing_backoff_then_resolved(self):
        self.authorize()
        cluster, real_restore = self.f.cluster, self.f.cluster.conditional_restore
        pending = []

        def timeout(**kwargs):
            pending.append(kwargs)
            raise TimeoutError("request completion is unknown")

        cluster.conditional_restore = timeout
        executor = self.worker("executor", ExecutorAdapter(self.f.adapters))
        self.assertEqual(executor.once()["status"], "accepted")
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")

        def receipt():
            return self.f.store.db.execute("""SELECT r.attempts, r.available FROM transport_receipts r
                JOIN outbox o ON o.id=r.event_id WHERE o.topic='executor.reconcile'""").fetchone()

        gaps = []
        for _ in range(2):
            self.assertEqual(executor.once()["status"], "pending")
            attempts, available = receipt()
            gaps.append(available - time.time())
            time.sleep(max(0, available - time.time()) + 0.05)
        self.assertEqual(receipt()[0], 2)
        self.assertGreater(gaps[1], gaps[0])
        self.assertEqual(len(self.f.ledger.results(self.proposal["action_id"])), 1)
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")
        # The original request lands late; the next delivery reports APPLIED.
        real_restore(**pending[0])
        self.assertEqual(executor.once()["status"], "accepted")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_remote_verification_binds_the_executed_template_and_detects_a_second_fault(self):
        from tests.test_verifier_pipeline import grpc_policy, grpc_samples
        self.authorize()
        self.worker("executor", ExecutorAdapter(self.f.adapters)).once()
        execution = self.f.store.action(self.proposal["action_id"])["execution_result"]
        expected = self.proposal["parameters"]["template_sha256"]
        self.verifier.policy_reader = lambda _: grpc_policy("a" * 64)  # Stale static policy hash.
        readings = grpc_samples(execution, expected)
        readings[-1].update(template_sha256="b" * 64, ready_replicas=0, grpc_status=14)
        self.verifier.sample_reader = lambda _: readings
        self.f.now += timedelta(seconds=15)
        self.assertEqual(self.worker("verifier", self.verifier).once()["status"], "accepted")
        self.assertEqual(self.state(), "FAILED")
        job = self.f.verification.get(execution["action_id"])
        self.assertEqual(job["policy"]["health"]["expected_template_sha256"], expected)
        self.assertEqual(job["outcome"]["status"], "NOT_RECOVERED")
        checks = {c["name"]: c for c in job["outcome"]["checks"]}
        self.assertIn("grpc_status=14", checks["service_health"]["detail"])
        self.assertIn("ready_replicas=0", checks["service_health"]["detail"])
        self.assertIn("b" * 64, checks["sample_configuration"]["detail"])
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_remote_verifier_dependency_requests_drive_the_complete_flow_without_shared_files(self):
        RemoteIngress(self.client("observer"))(self.incident)
        self.worker("planner", self.planner).once()
        action_id = self.f.store.incident(self.incident["incident_id"])["active_action_id"]
        proposal = self.f.store.action(action_id)["proposal"]
        verifier = self.worker("verifier", self.verifier)
        guard = self.worker("guard", self.guard)
        self.assertEqual(guard.once()["status"], "idle")
        self.assertEqual(verifier.once()["status"], "accepted")
        self.assertEqual(self.client("guard").post("dependencies", {"proposal": proposal})["dependencies"]["status"], "CLEAR")
        self.guard.dependency_reader = lambda p: self.client("guard").post("dependencies", {"proposal": p})["dependencies"]
        self.f.authorizer.dependency_reader = lambda p: self.client("executor").post("dependencies", {"proposal": p})["dependencies"]
        self.assertEqual(guard.once()["status"], "accepted")
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        self.f.now += timedelta(seconds=1)
        self.approval_client("operator").post("create", {
            "action_id": action_id, "proposal_sha256": digest(proposal),
            "policy_version": self.f.policy["policy_version"],
            "expires_at": (self.f.now + timedelta(seconds=120)).isoformat()})
        self.f.now += timedelta(seconds=1)
        self.assertEqual(guard.once()["status"], "idle")
        self.assertEqual(verifier.once()["status"], "accepted")
        self.assertEqual(guard.once()["status"], "accepted")
        self.worker("executor", ExecutorAdapter(self.f.adapters)).once()
        self.assertEqual(self.state(), "VERIFYING")
        self.f.now = self.f.now.replace(minute=1, second=10)
        verifier.once()
        self.assertEqual(self.state(), "RECOVERED")
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_dependency_lookup_enforces_role_and_exact_active_proposal(self):
        self.plan()
        with self.assertRaises(ProtocolError) as wrong_role:
            self.client("planner").post("dependencies", {"proposal": self.proposal})
        self.assertEqual(wrong_role.exception.status, 403)
        changed = dict(self.proposal, rationale="changed intent")
        with self.assertRaises(ProtocolError) as changed_binding:
            self.client("guard").post("dependencies", {"proposal": changed})
        self.assertEqual(changed_binding.exception.status, 409)

    def test_lost_model_result_response_replays_saved_body_after_worker_restart(self):
        RemoteIngress(self.client("observer"))(self.incident)
        client = self.client("planner")
        original = client.post
        lost = [False]
        def post(operation, body):
            response = original(operation, body)
            if operation == "result" and not lost[0]:
                lost[0] = True
                raise TimeoutError("lost committed response")
            return response
        client.post = post
        first = self.worker("planner", self.planner, client=client)
        with self.assertRaises(TimeoutError):
            first.once()
        restarted = self.worker("planner", self.planner)
        self.assertEqual(restarted.once(), {"status": "accepted", "replay": True})
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.state(), "VALIDATING")

    def test_heartbeat_renews_a_long_provider_call_and_blocks_a_competing_claim(self):
        RemoteIngress(self.client("observer"))(self.incident)
        def long_call(context):
            time.sleep(2.2)  # Longer than the server's initial delivery lease.
            self.assertIsNone(self.client("replacement").post("claim", {})["delivery"])
            return mock_provider(context)
        self.provider.side_effect = long_call
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        self.assertEqual(self.provider.call_count, 1)

    def test_expired_saved_result_is_rebound_without_recalling_the_provider(self):
        RemoteIngress(self.client("observer"))(self.incident)
        worker = self.worker("planner", self.planner)
        with patch.object(worker, "_send", side_effect=TimeoutError("before result delivery")):
            with self.assertRaises(TimeoutError):
                worker.once()
        time.sleep(3.1)  # Initial lease plus bounded redelivery backoff.
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.state(), "VALIDATING")

    def test_revoked_approval_blocks_remote_execution_with_zero_writes(self):
        self.authorize()
        self.approval_client("operator").post("revoke", {
            "action_id": self.proposal["action_id"], "proposal_sha256": digest(self.proposal),
            "approval_id": self.created["approval_id"], "reason": "withdrawn"})
        self.assertEqual(self.worker("executor", ExecutorAdapter(self.f.adapters)).once()["status"], "idle")
        self.assertEqual(self.f.cluster.write_attempts, [])
        self.assertNotEqual(self.state(), "READY")

    def test_crash_after_repair_before_transport_save_replays_executor_journal_once(self):
        self.authorize()
        worker = self.worker("executor", ExecutorAdapter(self.f.adapters))
        with patch.object(worker.journal, "save", side_effect=OSError("worker crash after execution")):
            with self.assertRaises(OSError):
                worker.once()
        self.assertEqual(len(self.f.cluster.writes), 1)
        self.assertEqual(self.state(), "READY")
        time.sleep(1.1)
        restarted = self.worker("executor", ExecutorAdapter(self.f.adapters))
        self.assertEqual(restarted.once()["status"], "accepted")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_post_journal_remote_revocation_fences_the_write(self):
        self.authorize()
        original = self.f.ledger.start_write
        def revoke_after_journal(*args):
            original(*args)
            self.approval_client("operator").post("revoke", {
                "action_id": self.proposal["action_id"], "proposal_sha256": digest(self.proposal),
                "approval_id": self.created["approval_id"], "reason": "withdraw during handoff"})
        worker = self.worker("executor", ExecutorAdapter(self.f.adapters))
        with patch.object(self.f.ledger, "start_write", side_effect=revoke_after_journal):
            with self.assertRaises(ProtocolError) as error:
                worker.once()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.f.cluster.write_attempts, [])
        self.assertNotEqual(self.state(), "VERIFYING")

    def test_invalid_provider_exhausts_to_review_without_authorizing_a_write(self):
        RemoteIngress(self.client("observer"))(self.incident)
        self.provider.side_effect = lambda _: {}
        self.planner.options["max_attempts"] = 1
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        self.assertEqual(self.state(), "NEEDS_REVIEW")
        self.assertEqual(self.worker("guard", self.guard).once()["status"], "idle")
        self.assertEqual(self.f.cluster.write_attempts, [])

    def test_no_health_evidence_reaches_fixed_deadline_after_worker_restart(self):
        self.authorize()
        self.worker("executor", ExecutorAdapter(self.f.adapters)).once()
        self.verifier.sample_reader = Mock(side_effect=OSError("collector unavailable"))
        with self.assertRaises(OSError):
            self.worker("verifier", self.verifier).once()
        self.f.now += timedelta(seconds=500)
        time.sleep(1.1)
        self.assertEqual(self.worker("verifier", self.verifier).once()["status"], "accepted")
        self.assertEqual(self.state(), "NEEDS_REVIEW")
        self.assertEqual(self.verifier.sample_reader.call_count, 1)
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_wrong_role_cannot_renew_a_real_http_delivery(self):
        RemoteIngress(self.client("observer"))(self.incident)
        delivery = self.client("planner").post("claim", {})["delivery"]
        with self.assertRaises(ProtocolError) as error:
            self.client("guard").post("renew", {key: delivery[key] for key in ("delivery_id", "lease_token")})
        self.assertEqual(error.exception.status, 403)

    def test_planner_service_entrypoint_uses_network_and_only_its_private_state(self):
        from runtime.worker_service import main
        RemoteIngress(self.client("observer"))(self.incident)
        baseline = self.f.path / "baseline.json"
        baseline.write_text(json.dumps(self.f.baseline))
        model = self.f.path / "model.json"
        model.write_text(json.dumps({"model": "scripted", "base_url": "http://127.0.0.1:11434", "timeout": 1}))
        config = self.f.path / "worker.json"
        state = self.f.path / "cli-planner"
        config.write_text(json.dumps({"role": "planner", "url": self.url, "identity": "planner",
            "token_env": "TEST_PLANNER_CREDENTIAL", "state_directory": str(state),
            "model_config": str(model), "baselines": str(baseline)}))
        with patch.dict("os.environ", {"TEST_PLANNER_CREDENTIAL": self.services["planner"][1]}), \
                patch("runtime.worker_service.OllamaProvider", return_value=self.provider), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(config), "--once"]), 0)
        self.assertEqual(self.state(), "VALIDATING")
        self.assertTrue((state / "planner.db").is_file())
        self.assertTrue((state / "transport.db").is_file())
        self.assertEqual(self.provider.call_count, 1)

    def test_observer_cli_remote_mode_does_not_open_a_local_coordinator(self):
        from services.observer import cli
        observations = fixture("observations.json")["failed-rollout"]
        for observation in observations:
            for field in ("pods", "events", "logs", "collection_warnings"):
                observation.setdefault(field, [])
        collector = Mock(target=self.incident["target"])
        collector.collect.side_effect = observations
        policy = self.f.path / "observer-policy.json"
        policy.write_text(json.dumps(self.f.policy))
        state = self.f.path / "remote-observer.db"
        output = io.StringIO()
        with patch.dict("os.environ", {"SENTINEL_OBSERVER_TOKEN": self.services["observer"][1]}), \
                patch("services.observer.cli.KubectlReader"), \
                patch("services.observer.cli.KubernetesEvidenceCollector", return_value=collector), \
                patch("services.observer.cli.LocalCoordinatorSink") as local_sink, redirect_stdout(output):
            code = cli.main(["run", "--context", "test", "--cluster-id", self.incident["target"]["cluster_id"],
                "--namespace", self.incident["target"]["namespace"], "--deployment", self.incident["target"]["name"],
                "--state", str(state), "--policy", str(policy), "--coordinator-url", self.url,
                "--coordinator-identity", "observer", "--poll-count", str(len(observations)),
                "--interval-seconds", "0.001"])
        self.assertEqual(code, 0, output.getvalue())
        local_sink.assert_not_called()
        self.assertTrue(state.is_file())
        self.assertEqual(self.state(), "OPEN")


if __name__ == "__main__":
    unittest.main()
