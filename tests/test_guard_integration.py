import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runtime.guard_pipeline import KubectlStateReader, process_guard_requests
from services.coordinator.core import CoordinatorError, LocalEventBus, SQLiteStore, approve, handle

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class GuardIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(clock=lambda: self.time(36))
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        self.incident = fixture("messages/incident.json")
        self.proposal = fixture("messages/proposal.json")
        self.policy = fixture("policy.json")
        self.state = fixture("observations.json")["failed-rollout"][-1]
        self.baseline = fixture("baseline.json")
        self.dependencies = fixture("messages/dependency-result.json")
        self.aid = self.proposal["action_id"]
        for message in (self.incident, self.proposal):
            handle(message, self.store, self.bus)

    def time(self, second):
        return datetime(2026, 10, 10, 12, 0, second, tzinfo=timezone.utc)

    def process(self, second=35, **changes):
        arguments = dict(state_reader=lambda p: copy.deepcopy(self.state),
                         policy_reader=lambda p: copy.deepcopy(self.policy),
                         baseline_reader=lambda p: copy.deepcopy(self.baseline),
                         dependency_reader=lambda p: copy.deepcopy(self.dependencies),
                         clock=lambda: self.time(second))
        arguments.update(changes)
        return process_guard_requests(self.store, self.bus, **arguments)

    def execution_requests(self):
        return self.store.db.execute("SELECT * FROM outbox WHERE topic='executor.request' AND delivered=0").fetchall()

    def pending_guards(self):
        return self.store.db.execute("SELECT id FROM outbox WHERE topic='guard.request' AND delivered=0").fetchall()

    def test_real_guard_requires_approval_then_routes_once(self):
        result = self.process()[0]
        self.assertEqual(result["verdict"], "ESCALATE")
        self.assertEqual(self.execution_requests(), [])
        self.assertEqual(self.pending_guards(), [])
        record = approve(self.aid, "operator", self.store)
        self.assertEqual(record["policy_version"], self.policy["policy_version"])
        self.assertEqual(self.process(40)[0]["verdict"], "ALLOW")
        self.assertEqual(self.store.action(self.aid)["state"], "READY")
        self.assertEqual(len(self.execution_requests()), 1)
        self.assertEqual(self.process(41), [])
        self.assertEqual(len(self.execution_requests()), 1)

    def test_stale_state_after_approval_cannot_route_execution(self):
        self.process()
        approve(self.aid, "operator", self.store)
        self.state["deployment"]["metadata"]["resourceVersion"] = "changed"
        result = self.process(40)[0]
        self.assertEqual(result["verdict"], "REJECT")
        self.assertIn("STALE_RESOURCE_VERSION", result["reason_codes"])
        self.assertEqual(self.execution_requests(), [])

    def test_forbidden_target_is_rejected_and_cannot_be_approved(self):
        self.policy["targets"] = []
        self.assertEqual(self.process()[0]["verdict"], "REJECT")
        with self.assertRaises(CoordinatorError):
            approve(self.aid, "operator", self.store)
        self.assertEqual(self.execution_requests(), [])

    def test_blocked_dependencies_prevent_execution_even_after_approval(self):
        self.process()
        approve(self.aid, "operator", self.store)
        self.dependencies["status"] = "BLOCKED"
        self.assertEqual(self.process(40)[0]["verdict"], "REJECT")
        self.assertEqual(self.execution_requests(), [])

    def test_missing_dependency_provider_escalates_without_assuming_clear(self):
        result = self.process(dependency_reader=None)[0]
        self.assertEqual(result["verdict"], "ESCALATE")
        self.assertIn("DEPENDENCY_UNKNOWN", result["reason_codes"])
        self.assertEqual(self.execution_requests(), [])

    def test_stored_dependency_result_is_consumed_without_fixture_provider(self):
        handle(self.dependencies, self.store, self.bus)
        results = self.process(dependency_reader=None)
        self.assertTrue(results)
        self.assertTrue(all(item["reason_codes"] == ["OPERATOR_APPROVAL_REQUIRED"] for item in results))
        self.assertEqual(self.pending_guards(), [])

    def test_policy_change_requires_matching_new_approval(self):
        self.process()
        approve(self.aid, "operator", self.store)
        self.policy["policy_version"] = "policy-v2"
        result = self.process(40)[0]
        self.assertEqual(result["verdict"], "ESCALATE")
        self.assertIn("APPROVAL_MISMATCH", result["reason_codes"])
        self.assertEqual(self.execution_requests(), [])
        self.store.clock = lambda: self.time(41)
        renewed = approve(self.aid, "operator", self.store)
        self.assertEqual(renewed["policy_version"], "policy-v2")
        self.assertEqual(self.process(42)[0]["verdict"], "ALLOW")

    def test_unavailable_state_leaves_request_pending(self):
        def unavailable(proposal):
            raise RuntimeError("Kubernetes unavailable")
        with self.assertRaises(RuntimeError):
            self.process(state_reader=unavailable)
        self.assertEqual(len(self.pending_guards()), 1)
        self.assertIsNone(self.store.action(self.aid)["guard_result"])
        self.assertEqual(self.execution_requests(), [])
        self.assertEqual(self.process()[0]["verdict"], "ESCALATE")

    def test_result_and_acknowledgment_roll_back_together(self):
        with patch("runtime.guard_pipeline.acknowledge", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaises(RuntimeError):
                self.process()
        self.assertEqual(len(self.pending_guards()), 1)
        self.assertIsNone(self.store.action(self.aid)["guard_result"])
        self.assertEqual(self.store.action(self.aid)["state"], "VALIDATING")
        self.assertEqual(self.process()[0]["verdict"], "ESCALATE")

    def test_restart_processes_saved_requests_and_approvals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with SQLiteStore(path, clock=lambda: self.time(36)) as first:
                for message in (self.incident, self.proposal):
                    handle(message, first, LocalEventBus())
                readers = dict(state_reader=lambda p: self.state, policy_reader=lambda p: self.policy,
                               baseline_reader=lambda p: self.baseline, dependency_reader=lambda p: self.dependencies)
                process_guard_requests(first, LocalEventBus(), **readers, clock=lambda: self.time(35))
                approve(self.aid, "operator", first)
            with SQLiteStore(path) as second:
                results = process_guard_requests(second, LocalEventBus(), **readers, clock=lambda: self.time(40))
                self.assertEqual(results[0]["verdict"], "ALLOW")
                self.assertEqual(second.action(self.aid)["state"], "READY")

    def test_live_reader_uses_explicit_context_and_read_only_command(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(self.state["deployment"]), "")
        reader = KubectlStateReader("kind-sentinel-lab", "sentinel-lab", runner=runner, clock=lambda: self.time(35))
        snapshot = reader(self.proposal)
        self.assertEqual(snapshot["cluster_id"], "sentinel-lab")
        self.assertEqual(calls[0], ["kubectl", "--context", "kind-sentinel-lab", "--namespace", "sentinel-demo",
                                  "get", "deployment", "demo-api", "-o", "json"])
        result = self.process(state_reader=reader)[0]
        self.assertEqual(result["verdict"], "ESCALATE")

    def test_live_reader_rejects_different_cluster_and_api_failure(self):
        reader = KubectlStateReader("other", "other")
        with self.assertRaises(ValueError):
            reader(self.proposal)
        reader = KubectlStateReader("kind-sentinel-lab", "sentinel-lab", runner=lambda *a, **k:
                                    subprocess.CompletedProcess(a[0], 1, "", "connection refused"))
        with self.assertRaises(RuntimeError):
            reader(self.proposal)


if __name__ == "__main__":
    unittest.main()
