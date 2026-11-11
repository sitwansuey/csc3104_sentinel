"""HTTP approval authority with real SQLite, Guard and Executor; fake Kubernetes."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from runtime.approval_http import ApprovalClient, handler
from runtime.executor_pipeline import GuardAuthorizer, process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.service_identity import ProtocolError, load_identities
from services.coordinator.approvals import load_permissions
from services.coordinator.core import CoordinatorError, LocalEventBus, SQLiteStore, dispatch_pending, handle
from services.coordinator.read import read_records
from services.coordinator.store import _digest
from services.executor.core import ExecutionAdapters, execute
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from services.guard.core import evaluate_with_report
from tests.support.executor_fake import FakeDeploymentAPI

ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class ApprovalAccessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.now = datetime(2026, 10, 10, 12, 0, 36, tzinfo=timezone.utc)
        self.db = self.path / "coordinator.db"
        self.store = SQLiteStore(self.db, clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.bus = LocalEventBus()
        self.proposal = sample("messages/proposal.json")
        self.policy = sample("policy.json")
        self.baseline = sample("baseline.json")
        self.dependencies = sample("messages/dependency-result.json")
        self.cluster = FakeDeploymentAPI(sample("observations.json")["failed-rollout"][-1], clock=lambda: self.now)
        for message in (sample("messages/incident.json"), self.proposal, sample("messages/guard-result.json")):
            handle(message, self.store, self.bus)
        self.services = {"operator": ("operator", "o" * 40), "guard": ("guard", "g" * 40),
                         "executor": ("executor", "e" * 40), "other": ("operator", "x" * 40)}
        target = self.proposal["target"]
        self.permissions = {
            "operator": [{"target": target, "operations": ["create", "read", "revoke", "history"]}],
            "guard": [{"target": target, "operations": ["read"]}],
            "executor": [{"target": target, "operations": ["read"]}],
            "other": [{"target": dict(target, uid="other-uid"), "operations": ["create", "read", "revoke", "history"]}],
        }
        self.start_server()
        self.addCleanup(self.stop_server)
        self.binding = {"action_id": self.proposal["action_id"], "proposal_sha256": _digest(self.proposal)}

    def start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(
            self.db, self.services, self.permissions, clock=lambda: self.now))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def client(self, name="operator", token=None):
        return ApprovalClient(self.url, name, token or self.services[name][1])

    def create(self, **overrides):
        return self.client().post("create", dict(
            self.binding, policy_version=self.policy["policy_version"],
            expires_at=(self.now + timedelta(seconds=60)).isoformat(), **overrides))

    def revoke(self, created, reason="Operator withdrew approval"):
        return self.client().post("revoke", dict(self.binding, approval_id=created["approval_id"], reason=reason))

    def guard(self):
        self.now += timedelta(seconds=1)
        return process_guard_requests(
            self.store, self.bus, state_reader=lambda p: self.cluster.read(p["target"]),
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies, clock=lambda: self.now)

    def adapters(self):
        ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.addCleanup(ledger.close)
        authorizer = GuardAuthorizer(
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies, approval_reader=self.client("executor"))
        return ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(self.path / "locks"),
                                 cluster=self.cluster, authorizer=authorizer, clock=lambda: self.now)

    def assert_status(self, status, function):
        with self.assertRaises(ProtocolError) as error:
            function()
        self.assertEqual(error.exception.status, status)

    def test_invalid_credentials_and_wrong_target_cannot_create_read_revoke_or_audit(self):
        created = self.create()
        bodies = {"create": dict(self.binding, policy_version=self.policy["policy_version"],
                                 expires_at=(self.now + timedelta(seconds=60)).isoformat()),
                  "read": self.binding, "history": self.binding,
                  "revoke": dict(self.binding, approval_id=created["approval_id"], reason="test")}
        for operation, body in bodies.items():
            with self.subTest(operation=operation):
                self.assert_status(401, lambda: self.client(token="wrong").post(operation, body))
                self.assert_status(403, lambda: self.client("other").post(operation, body))
        self.assertEqual(self.client("guard")(self.proposal), created["approval"])
        for operation in ("create", "revoke", "history"):
            self.assert_status(403, lambda: self.client("executor").post(operation, bodies[operation]))
        self.assertIsNotNone(self.store.current_approval(self.proposal["action_id"]))

    def test_bound_creation_rejects_changed_proposal_policy_identity_and_excessive_expiry(self):
        request = dict(self.binding, policy_version=self.policy["policy_version"],
                       expires_at=(self.now + timedelta(seconds=60)).isoformat())
        for key, value in (("proposal_sha256", "0" * 64), ("policy_version", "other")):
            self.assert_status(409, lambda: self.client().post("create", dict(request, **{key: value})))
        self.assert_status(400, lambda: self.client().post("create", dict(request, operator_id="spoofed")))
        self.assert_status(400, lambda: self.client().post("create", dict(
            request, expires_at=(self.now + timedelta(days=1)).isoformat())))
        self.assertIsNone(self.store.approval(self.proposal["action_id"]))

    def test_create_retry_and_restart_preserve_exact_record_without_duplicate_work(self):
        created = self.create()
        count = len(self.store.pending_routes())
        self.assertEqual(self.create(), created)
        self.assertEqual(len(self.store.pending_routes()), count)
        self.stop_server()
        self.store.close()
        self.store = SQLiteStore(self.db, clock=lambda: self.now)
        self.start_server()
        self.assertEqual(self.client("executor")(self.proposal), created["approval"])
        self.assertEqual(self.client().post("history", self.binding)["history"][0]["approval"], created["approval"])
        public = read_records(self.db)["approvals"][0]["record"]
        self.assertTrue(public["protected"])
        self.assertNotIn("approved_at", public)

    def test_expired_lookup_returns_no_authority_and_cancels_queued_execution(self):
        created = self.create()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        self.now = datetime.fromisoformat(created["approval"]["expires_at"])
        self.assertIsNone(self.client("executor")(self.proposal))
        dispatch_pending(self.store, self.bus)
        self.assertFalse(self.store.pending_routes("executor.request"))
        self.assertEqual(self.cluster.writes, [])
        self.assertEqual(len(self.client().post("history", self.binding)["history"]), 1)

    def test_revocation_persists_restart_retires_work_and_allows_new_bound_approval(self):
        created = self.create()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        revoked = self.revoke(created)
        self.assertEqual(self.revoke(created), revoked)
        self.assertIsNone(self.client("guard")(self.proposal))
        self.store.close()
        self.store = SQLiteStore(self.db, clock=lambda: self.now)
        dispatch_pending(self.store, self.bus)
        self.assertFalse(self.store.pending_routes("executor.request"))
        self.assertEqual(self.guard()[0]["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        replacement = self.create()
        self.assertNotEqual(replacement["approval_id"], created["approval_id"])
        self.assert_status(409, lambda: self.revoke(created))
        history = self.client().post("history", self.binding)["history"]
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["revocation"], revoked["revocation"])
        self.assertIsNone(history[1]["revocation"])
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")

    def test_delayed_guard_allow_cannot_restore_revoked_authority(self):
        created = self.create()
        self.now += timedelta(seconds=1)
        decision, report = evaluate_with_report(
            self.proposal, self.cluster.read(self.proposal["target"]), self.policy,
            self.baseline, self.dependencies, created["approval"], now=self.now)
        self.store.save_guard_report(self.proposal["action_id"], report)
        self.revoke(created)
        with self.assertRaises(CoordinatorError):
            handle(decision, self.store, self.bus)
        self.assertNotEqual(self.store.action(self.proposal["action_id"])["state"], "READY")

    def test_authenticated_approval_real_guard_executor_and_revocation_cutoff(self):
        created = self.create()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        results = process_executor_requests(self.store, self.bus, adapters=self.adapters())
        self.assertEqual(results[0]["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertEqual(self.store.incident(self.proposal["incident_id"])["state"], "VERIFYING")
        self.assert_status(409, lambda: self.revoke(created))

    def test_final_remote_lookup_after_journal_blocks_revoked_approval(self):
        created = self.create()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        adapters = self.adapters()
        original = adapters.ledger.start_write

        def revoke_after_journal(*args):
            original(*args)
            self.revoke(created)

        with patch.object(adapters.ledger, "start_write", side_effect=revoke_after_journal):
            result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertIn("withdrawn", result["details"])

    def test_final_remote_lookup_fails_closed_when_authority_becomes_unavailable(self):
        self.create()
        self.assertEqual(self.guard()[0]["verdict"], "ALLOW")
        adapters = self.adapters()
        original = adapters.ledger.start_write

        def disconnect_after_journal(*args):
            original(*args)
            adapters.authorizer.approval_reader.post = lambda *args: (_ for _ in ()).throw(
                ProtocolError(503, "Approval authority unavailable"))

        with patch.object(adapters.ledger, "start_write", side_effect=disconnect_after_journal):
            result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_legacy_local_demo_approval_is_not_promoted_by_authenticated_lookup(self):
        from services.coordinator.core import approve
        approve(self.proposal["action_id"], "local-demo", self.store)
        self.assertIsNotNone(self.store.approval(self.proposal["action_id"]))
        self.assertIsNone(self.client("executor")(self.proposal))

    def test_configuration_rejects_wildcard_target_and_write_grants_for_executor(self):
        path = self.path / "permissions.json"
        for broken in (
                {"executor": [{"target": self.proposal["target"], "operations": ["create"]}]},
                {"operator": [{"target": dict(self.proposal["target"], uid="*"), "operations": ["read"]}]}):
            path.write_text(json.dumps(broken), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_permissions(path, self.services)
        path.write_text(json.dumps(self.permissions), encoding="utf-8")
        self.assertEqual(load_permissions(path, self.services), self.permissions)

    def test_shared_identity_loader_uses_environment_and_rejects_weak_or_reused_tokens(self):
        path = self.path / "identities.json"
        path.write_text(json.dumps({"operator": {"role": "operator", "token_env": "TEST_APPROVAL_TOKEN"}}))
        with patch.dict("os.environ", {"TEST_APPROVAL_TOKEN": "a" * 40}):
            self.assertEqual(load_identities(path, {"operator"}), {"operator": ("operator", "a" * 40)})
        with patch.dict("os.environ", {"TEST_APPROVAL_TOKEN": "short"}):
            with self.assertRaises(ValueError):
                load_identities(path, {"operator"})


if __name__ == "__main__":
    unittest.main()
