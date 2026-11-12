"""Actual component algorithms over loopback HTTP, using fake Kubernetes only."""
from contextlib import ExitStack
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
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
from runtime.worker_health import PreflightFailed, WorkerHealth
from runtime.worker_http import WorkerClient
from runtime.worker_transport import ProtocolError, digest
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from services.observer.core import detect
from services.planner.core import mock_provider
from services.coordinator.read import read_records
from services.planner.store import PlannerStore
from services.verifier.core import inspect_dependencies
from scripts import trace_view
from shared import log, tracing


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

    def worker(self, role, adapter, *, client=None, before_claim=None):
        journal = WorkerJournal(self.f.path / (role + "-transport.db"))
        self.stack.callback(journal.close)
        return RemoteWorker(client or self.client(role), journal, adapter, renewal_interval=0.1,
                            before_claim=before_claim)

    def state(self):
        return self.f.store.incident(self.incident["incident_id"])["state"]

    def captured_logs(self):
        stream = io.StringIO()
        self.stack.enter_context(log.configured("test", level="DEBUG", stream=stream))
        return stream

    def secrets(self):
        values = [token for _, token in (*self.services.values(), *self.approval_services.values())]
        values += [row[0] for row in self.f.store.db.execute(
            "SELECT lease_token FROM transport_receipts WHERE lease_token IS NOT NULL")]
        return values

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

    def test_full_loopback_flow_logs_correlate_one_action_and_never_contain_secrets(self):
        stream = self.captured_logs()
        self.test_all_six_components_recover_over_http_with_managed_approval_and_owned_stores()
        text = stream.getvalue()
        records = [json.loads(line) for line in text.splitlines()]
        for secret in self.secrets():
            self.assertNotIn(secret, text)
        for fragment in ("Bearer", "lease_token", "payload", self.proposal["parameters"]["template_sha256"]):
            self.assertNotIn(fragment, text)
        action_id = self.proposal["action_id"]
        loggers = {r["logger"] for r in records if r.get("action_id") == action_id}
        self.assertLessEqual({"sentinel.coordinator", "sentinel.worker_http", "sentinel.approval_http"}, loggers)
        logged = [(r["seq"], r["to_state"]) for r in records if r["event"] == "coordinator.state.transition"
                  and r.get("action_id") == action_id]
        journal = [(t["seq"], t["to_state"]) for t in reversed(read_records(self.f.path / "coordinator.db")["transitions"])
                   if t["action_id"] == action_id]
        self.assertEqual(logged, journal)
        self.assertEqual(logged[-1][1], "RECOVERED")
        claims = [r for r in records if r["event"] == "http.access" and r["operation"] == "claim"
                  and r["outcome"] == "claimed"]
        self.assertTrue(claims and all({"delivery_id", "event_id", "topic", "attempt"} <= set(r) for r in claims))

    def test_full_loopback_flow_is_one_connected_trace(self):
        stream = self.captured_logs()
        self.test_all_six_components_recover_over_http_with_managed_approval_and_owned_stores()
        text = stream.getvalue()
        records = [json.loads(line) for line in text.splitlines()]
        ends = [r for r in records if r["event"] == "span.end"]
        self.assertEqual(len({r["trace_id"] for r in ends}), 1)
        (trace,) = trace_view.build(records).values()
        (root,) = trace["roots"]
        self.assertEqual((root["span_name"], root.get("missing_parent")), ("observer.ingest", False))
        processed = [r.get("topic") for r in ends if r["span_name"] == "worker.process"]
        self.assertEqual(sorted(processed), ["executor.request", "guard.request", "guard.request",
                                             "planner.request", "verifier.request"])
        self.assertLessEqual({"coordinator.ingest", "coordinator.result", "coordinator.renew", "coordinator.record",
                              "worker.result"}, {r["span_name"] for r in ends})
        moves = [r["to_state"] for r in trace["transitions"] if r.get("action_id") == self.proposal["action_id"]]
        self.assertEqual(moves[-1], "RECOVERED")
        for secret in self.secrets():
            self.assertNotIn(secret, text)
        output = io.StringIO()
        log_file = self.f.path / "trace.log"
        log_file.write_text(text, encoding="utf-8")
        self.assertEqual(trace_view.main([str(log_file), "--db", str(self.f.path / "coordinator.db")],
                                         stdout=output), 0)
        self.assertIn("-> RECOVERED", output.getvalue())
        self.assertIn("journal for " + self.incident["incident_id"], output.getvalue())
        self.assertNotIn("parent not in these logs", output.getvalue())

    def test_a_result_replayed_after_restart_keeps_the_original_parent(self):
        stream = self.captured_logs()
        RemoteIngress(self.client("observer"))(self.incident)
        worker = self.worker("planner", self.planner)
        with patch.object(worker, "_send", side_effect=TimeoutError("worker stopped before sending")):
            with self.assertRaises(TimeoutError):
                worker.once()
        (saved,) = worker.journal.pending()
        parent = tracing.parse_traceparent(saved["delivery"]["traceparent"])
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        ends = [json.loads(line) for line in stream.getvalue().splitlines()]
        ends = {r["span_name"]: r for r in ends if r["event"] == "span.end"}
        process, result, server = ends["worker.process"], ends["worker.result"], ends["coordinator.result"]
        self.assertEqual(process["status"], "error")
        for span in (process, result):
            self.assertEqual((span["trace_id"], span["parent_span_id"]), (parent.trace_id, parent.span_id))
        self.assertEqual(server["parent_span_id"], result["span_id"])
        self.assertEqual(self.provider.call_count, 1)

    def test_worker_http_denials_and_conflicts_log_exactly_one_line_without_secrets(self):
        stream = self.captured_logs()
        RemoteIngress(self.client("observer"))(self.incident)
        delivery = self.client("planner").post("claim", {})["delivery"]
        lease = {key: delivery[key] for key in ("delivery_id", "lease_token")}
        wrong = "w" * 40
        cases = (
            (401, "auth.denied", "invalid_credentials",
             lambda: WorkerClient(self.url, "planner", wrong, timeout=2).post("claim", {})),
            (403, "auth.denied", "role_not_permitted", lambda: self.client("observer").post("claim", {})),
            (403, "auth.denied", "not_lease_owner", lambda: self.client("guard").post("renew", lease)),
            (409, "http.access", "lease_replaced", lambda: self.client("planner").post(
                "result", {"delivery_id": delivery["delivery_id"], "lease_token": "f" * 32, "result": {}})),
        )
        for status, event, reason, call in cases:
            with self.subTest(status=status, reason=reason):
                offset = len(stream.getvalue())
                with self.assertRaises(ProtocolError) as error:
                    call()
                self.assertEqual(error.exception.status, status)
                (record,) = [json.loads(line) for line in stream.getvalue()[offset:].splitlines()]
                self.assertEqual((record["event"], record["http_status"], record["reason"]), (event, status, reason))
                self.assertEqual(record["level"], "warning")
                self.assertEqual("identity" in record, status != 401)
        text = stream.getvalue()
        for secret in (*self.secrets(), wrong, delivery["lease_token"], "f" * 32, "Bearer"):
            self.assertNotIn(secret, text)

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

    def test_older_own_uncertainty_after_a_peer_report_is_retried_not_acknowledged(self):
        self.authorize()
        cluster, real_restore = self.f.cluster, self.f.cluster.conditional_restore
        pending = []

        def timeout(**kwargs):
            pending.append(kwargs)
            raise TimeoutError("request completion is unknown")

        cluster.conditional_restore = timeout
        host_a = self.worker("executor", ExecutorAdapter(self.f.adapters))
        self.assertEqual(host_a.once()["status"], "accepted")
        own = self.f.ledger.results(self.proposal["action_id"])
        self.assertEqual([result["status"] for result in own], ["NEEDS_RECONCILIATION"])
        # Host B: its own ledger, lock directory and transport journal.
        ledger_b = SQLiteExecutionLedger(self.f.path / "host-b-ledger.db")
        self.stack.callback(ledger_b.close)
        adapters_b = replace(self.f.adapters, ledger=ledger_b,
                             coordination=LocalTargetCoordinator(self.f.path / "host-b-locks"))
        journal_b = WorkerJournal(self.f.path / "host-b-transport.db")
        self.stack.callback(journal_b.close)
        host_b = RemoteWorker(self.client("executor"), journal_b, ExecutorAdapter(adapters_b), renewal_interval=0.1)
        self.f.now += timedelta(seconds=61)
        self.assertEqual(host_b.once()["status"], "accepted")
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")
        # Host A deduplicates to its older saved result; the command must stay open.
        self.f.now += timedelta(seconds=61)
        self.assertEqual(host_a.once()["status"], "pending")
        self.assertEqual(self.f.ledger.results(self.proposal["action_id"]), own)
        unfinished = self.f.store.db.execute(
            "SELECT COUNT(*) FROM outbox o LEFT JOIN transport_receipts r ON r.event_id=o.id "
            "WHERE o.topic='executor.reconcile' AND r.finished IS NULL").fetchone()[0]
        self.assertEqual(unfinished, 1)
        # The original request lands late; a retry of the open command reports APPLIED.
        real_restore(**pending[0])
        available = self.f.store.db.execute("""SELECT MAX(r.available) FROM transport_receipts r
            JOIN outbox o ON o.id=r.event_id WHERE o.topic='executor.reconcile'""").fetchone()[0]
        time.sleep(max(0, available - time.time()) + 0.05)
        self.assertEqual(host_a.once()["status"], "accepted")
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

    def failing_prerequisite(self):
        return Mock(side_effect=PreflightFailed("MODEL_UNAVAILABLE"))

    def recording_client(self, role, operations):
        client = self.client(role)
        original = client.post

        def post(operation, body):
            operations.append(operation)
            return original(operation, body)
        client.post = post
        return client

    def test_a_failing_claim_prerequisite_claims_nothing(self):
        RemoteIngress(self.client("observer"))(self.incident)
        operations, prerequisite = [], self.failing_prerequisite()
        worker = self.worker("planner", self.planner, client=self.recording_client("planner", operations),
                             before_claim=prerequisite)
        with self.assertRaises(PreflightFailed):
            worker.once()
        # A worker with no skew bound still measures its clock once before its first claim
        # (a Coordinator with its own bound refuses a claim without an estimate); it never claims.
        self.assertEqual((operations, prerequisite.call_count, self.provider.call_count), (["clock"], 1, 0))
        self.assertEqual(self.client("replacement").post("claim", {})["delivery"]["topic"], "planner.request")

    def test_a_failing_claim_prerequisite_never_holds_back_a_saved_result(self):
        RemoteIngress(self.client("observer"))(self.incident)
        client = self.client("planner")
        original = client.post

        def post(operation, body):
            if operation == "result":
                raise TimeoutError("the result request was lost before the Coordinator read it")
            return original(operation, body)
        client.post = post
        with self.assertRaises(TimeoutError):
            self.worker("planner", self.planner, client=client).once()
        self.assertEqual(self.state(), "OPEN")
        prerequisite = self.failing_prerequisite()
        restarted = self.worker("planner", self.planner, before_claim=prerequisite)
        self.assertEqual(restarted.once(), {"status": "accepted", "replay": False})
        self.assertEqual((prerequisite.call_count, self.provider.call_count), (0, 1))
        self.assertEqual(self.state(), "VALIDATING")

    def test_a_released_saved_result_is_claimed_back_without_the_prerequisite(self):
        RemoteIngress(self.client("observer"))(self.incident)
        worker = self.worker("planner", self.planner)
        with patch.object(worker, "_send", side_effect=TimeoutError("before result delivery")):
            with self.assertRaises(TimeoutError):
                worker.once()
        time.sleep(3.1)  # The lease expires, so the saved result is released and its request claimed again.
        prerequisite = self.failing_prerequisite()
        self.assertEqual(self.worker("planner", self.planner, before_claim=prerequisite).once()["status"], "accepted")
        self.assertEqual((prerequisite.call_count, self.provider.call_count), (0, 1))
        self.assertEqual(self.state(), "VALIDATING")

    def test_new_work_claimed_for_a_released_result_is_handed_back_when_the_prerequisite_fails(self):
        RemoteIngress(self.client("observer"))(self.incident)
        operations, prerequisite = [], self.failing_prerequisite()
        worker = self.worker("planner", self.planner, client=self.recording_client("planner", operations),
                             before_claim=prerequisite)
        with patch.object(worker.journal, "held", return_value=True):  # Some other saved result waits.
            with self.assertRaises(PreflightFailed):
                worker.once()
        self.assertEqual((operations, prerequisite.call_count, self.provider.call_count),
                         (["clock", "claim", "retry"], 1, 0))
        (row,) = worker.journal.db.execute("SELECT delivery, result FROM worker_deliveries").fetchall()
        self.assertEqual(tuple(row), (None, None))
        self.assertEqual(self.state(), "OPEN")

    def test_heartbeat_renews_a_long_provider_call_and_blocks_a_competing_claim(self):
        RemoteIngress(self.client("observer"))(self.incident)
        def long_call(context):
            time.sleep(2.2)  # Longer than the server's initial delivery lease.
            self.assertIsNone(self.client("replacement").post("claim", {})["delivery"])
            return mock_provider(context)
        self.provider.side_effect = long_call
        self.assertEqual(self.worker("planner", self.planner).once()["status"], "accepted")
        self.assertEqual(self.provider.call_count, 1)

    def readiness_during_a_long_pass(self, client, steps):
        """Run one planner pass whose model call advances the probe clock 20 s per step; return /readyz each step."""
        RemoteIngress(self.client("observer"))(self.incident)
        seen = []

        def long_call(context):
            for _ in range(steps):
                self.now[0] += 20
                time.sleep(0.6)  # Several 0.1 s lease renewals in real time.
                seen.append(self.health.ready())
            return mock_provider(context)
        self.provider.side_effect = long_call
        return self.worker("planner", self.planner, client=client), seen

    def probe(self):
        self.now = [1000.0]
        self.health = WorkerHealth(ready_seconds=30, clock=lambda: self.now[0])
        self.health.configured()

    def test_lease_renewals_keep_a_pass_longer_than_the_readiness_window_ready(self):
        # Live, a working agent Planner failed two Docker health checks in one 128 s pass: only
        # the end of a pass counted as contact. Each successful renewal now counts.
        self.probe()
        client = WorkerClient(self.url, "planner", self.services["planner"][1], timeout=2,
                              answered=self.health.contacted)
        worker, seen = self.readiness_during_a_long_pass(client, 4)  # 80 s of probe time, window 30 s.
        self.assertEqual(worker.once()["status"], "accepted")
        self.assertEqual([status for status, _ in seen], [200, 200, 200, 200])
        self.assertTrue(all(body["contact_age_seconds"] < 30 for _, body in seen), seen)

    def test_a_pass_whose_renewals_fail_turns_not_ready_after_the_readiness_window(self):
        self.probe()

        class RefusedRenewals(WorkerClient):
            renewals = 0

            def post(self, operation, body):
                if operation == "renew":
                    self.renewals += 1
                    if self.renewals > 1:  # The first renewal, on entering the lease, succeeds.
                        body = dict(body, lease_token="x" * 40)  # The Coordinator answers, but refuses it.
                return super().post(operation, body)
        client = RefusedRenewals(self.url, "planner", self.services["planner"][1], timeout=2,
                                 answered=self.health.contacted)
        worker, seen = self.readiness_during_a_long_pass(client, 2)
        with self.assertRaises(ProtocolError):  # The lost lease fails the pass.
            worker.once()
        self.assertGreater(client.renewals, 1)
        self.assertEqual(seen[0], (200, {"ready": True, "contact_age_seconds": 20.0}))
        self.assertEqual(seen[1], (503, {"ready": False, "contact_age_seconds": 40.0}))

    def test_only_a_coordinator_answer_of_200_counts_as_contact(self):
        answered = Mock()
        client = WorkerClient(self.url, "planner", self.services["planner"][1], timeout=2, answered=answered)
        self.assertIsNone(client.post("claim", {})["delivery"])
        self.assertEqual(answered.call_count, 1)
        with self.assertRaises(ProtocolError) as refused:  # Refused: answered, but not a successful exchange.
            client.post("renew", {"delivery_id": "d", "lease_token": "x" * 40})
        self.assertNotEqual(refused.exception.status, 200)
        with self.assertRaises(OSError):  # Unreachable: nothing answered.
            WorkerClient("http://127.0.0.1:9", "planner", "p" * 40, timeout=2, answered=answered).post("claim", {})
        self.assertEqual(answered.call_count, 1)

    def test_a_reply_that_is_not_a_200_json_object_is_not_contact(self):
        answered = Mock()
        client = WorkerClient("http://127.0.0.1:9", "planner", "p" * 40, timeout=2, answered=answered)

        def reply(status, raw):
            response = Mock(spec=["status", "read"], status=status)  # No headers: a minimal stand-in.
            response.read.return_value = raw
            return Mock(**{"getresponse.return_value": response})
        for status, raw in ((200, b"not json"), (200, b"[1]"), (200, b"\xff"), (500, b"Internal error"),
                            (503, b'{"error": "Coordinator unavailable"}')):
            with self.subTest(status=status, raw=raw), self.assertRaises(ProtocolError), \
                    patch("runtime.worker_http.HTTPConnection", return_value=reply(status, raw)):
                client.post("claim", {})
        self.assertEqual(answered.call_count, 0)
        with patch("runtime.worker_http.HTTPConnection", return_value=reply(200, b"{}")):
            client.post("claim", {})
        self.assertEqual(answered.call_count, 1)

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
        errors = io.StringIO()
        with patch.dict("os.environ", {"TEST_PLANNER_CREDENTIAL": self.services["planner"][1]}), \
                patch("runtime.worker_service.OllamaProvider", return_value=self.provider), \
                redirect_stdout(io.StringIO()), redirect_stderr(errors):
            self.assertEqual(main(["--config", str(config), "--once"]), 0)
        status = [record for record in map(json.loads, errors.getvalue().splitlines())
                  if record["event"] == "worker.status"]
        self.assertEqual([(s["status"], s["identity"], s["topic"]) for s in status],
                         [("accepted", "planner", "planner.request")])
        self.assertIn("delivery_id", status[0])
        for secret in self.secrets():
            self.assertNotIn(secret, errors.getvalue())
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
