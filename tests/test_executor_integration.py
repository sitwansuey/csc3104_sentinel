"""Real Coordinator/Guard/executor integration; Kubernetes remains a fake API."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.executor_pipeline import (
    GuardAuthorizer, execution_delivery_reviews, process_executor_requests,
    reconcile_pending_executions,
)
from runtime.guard_pipeline import process_guard_requests
from services.coordinator.core import LocalEventBus, SQLiteStore, approve, handle
from services.executor.core import ExecutionAdapters
from tests.support.executor_fake import FakeDeploymentAPI
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger


ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class ExecutorIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)
        self.now = datetime(2026, 10, 10, 12, 0, 35, tzinfo=timezone.utc)
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        self.addCleanup(lambda: self.store.close())
        self.ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.addCleanup(lambda: self.ledger.close())
        self.bus = LocalEventBus()
        self.proposal = fixture("messages/proposal.json")
        self.policy = fixture("policy.json")
        self.baseline = fixture("baseline.json")
        self.dependencies = fixture("messages/dependency-result.json")
        self.cluster = FakeDeploymentAPI(fixture("observations.json")["failed-rollout"][-1],
                                         clock=lambda: self.now)
        self.authorizer = GuardAuthorizer(
            policy_reader=lambda p: self.policy,
            baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies,
            approval_reader=lambda p: self.store.approval(p["action_id"]),
        )
        self.adapters = ExecutionAdapters(
            ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / "locks"),
            cluster=self.cluster, authorizer=self.authorizer, clock=lambda: self.now,
        )

    def guard(self):
        return process_guard_requests(
            self.store, self.bus, state_reader=lambda p: self.cluster.read(p["target"]),
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: self.dependencies, clock=lambda: self.now,
        )

    def prepare(self, *, approved=True):
        # Planner and dependency analysis supply fixtures, not implemented services.
        handle(fixture("messages/incident.json"), self.store, self.bus)
        handle(self.proposal, self.store, self.bus)
        self.assertEqual(self.guard()[0]["verdict"], "ESCALATE")
        if approved:
            self.now = self.now.replace(second=36)
            approve(self.proposal["action_id"], "local-test-operator", self.store)
            self.now = self.now.replace(second=40)
            self.assertEqual(self.guard()[0]["verdict"], "ALLOW")

    def run_worker(self):
        return process_executor_requests(self.store, self.bus, adapters=self.adapters)

    def state(self):
        return self.store.incident(self.proposal["incident_id"])["state"]

    def test_approved_execution_routes_to_verification_without_claiming_health(self):
        self.prepare()
        result = self.run_worker()[0]
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"],
                         self.baseline["pod_template"])
        self.assertEqual(self.cluster.snapshot["deployment"]["status"]["readyReplicas"], 0)
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertTrue(any(event["topic"] == "verifier.request" for event in self.bus.events))
        self.assertEqual(self.run_worker(), [])

    def test_missing_approval_never_routes_execution(self):
        self.prepare(approved=False)
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.state(), "AWAITING_APPROVAL")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_current_approval_is_read_instead_of_trusting_envelope_copy(self):
        self.prepare()
        # The queued request retains its approval, but the authority reader no longer does.
        self.authorizer.approval_reader = lambda p: None
        result = self.run_worker()[0]
        self.assertNotIn(result["status"], {"APPLIED", "NOOP"})
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertNotEqual(self.state(), "VERIFYING")

    def test_approval_cannot_override_a_fresh_hard_policy_rejection(self):
        self.prepare()
        self.policy["targets"] = []
        result = self.run_worker()[0]
        self.assertNotIn(result["status"], {"APPLIED", "NOOP"})
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.state(), "FAILED")

    def test_stale_deployment_is_rejected_without_changing_proposal_identity(self):
        self.prepare()
        original = deepcopy(self.proposal)
        self.cluster.snapshot["deployment"]["metadata"]["resourceVersion"] = "concurrent-update"
        result = self.run_worker()[0]
        self.assertNotIn(result["status"], {"APPLIED", "NOOP"})
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.store.action(self.proposal["action_id"])["proposal"], original)

    def test_later_guard_rejection_invalidates_a_pending_execution_command(self):
        self.prepare()
        decision = deepcopy(self.store.action(self.proposal["action_id"])["guard_result"])
        decision.update(verdict="REJECT", reason_codes=["TARGET_NOT_ALLOWED"],
                        checked_at="2026-10-10T12:00:41Z")
        handle(decision, self.store, self.bus)
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.state(), "REJECTED")

    def test_revocation_during_validation_is_checked_before_the_write(self):
        self.prepare()
        approval_reader = self.authorizer.approval_reader

        def revoke(proposal):
            approval = approval_reader(proposal)
            decision = deepcopy(self.store.action(proposal["action_id"])["guard_result"])
            decision.update(verdict="REJECT", reason_codes=["TARGET_NOT_ALLOWED"],
                            checked_at="2026-10-10T12:00:41Z")
            handle(decision, self.store, self.bus)
            return approval

        self.authorizer.approval_reader = revoke
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.state(), "REJECTED")

    def test_restart_replays_ledger_result_after_coordinator_commit_failure(self):
        self.prepare()
        with patch("runtime.executor_pipeline.record_message", side_effect=RuntimeError("commit interrupted")):
            with self.assertRaisesRegex(RuntimeError, "commit interrupted"):
                self.run_worker()
        self.assertEqual(self.state(), "READY")
        self.assertEqual(len(self.cluster.writes), 1)
        self.ledger.close()
        self.store.close()
        # Reopen both durable stores; keep the fake server's independent state.
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        self.ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.adapters = replace(self.adapters, ledger=self.ledger)
        result = self.run_worker()[0]
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_crash_after_kubernetes_write_reconciles_before_new_guard_evaluation(self):
        class ProcessCrash(BaseException):
            pass

        class CrashingAPI(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                super().conditional_restore(**kwargs)
                raise ProcessCrash("process died after the server accepted the write")

        self.cluster = CrashingAPI(self.cluster.snapshot, clock=lambda: self.now)
        self.adapters = replace(self.adapters, cluster=self.cluster)
        self.prepare()
        with self.assertRaises(ProcessCrash):
            self.run_worker()
        self.assertEqual(self.state(), "READY")
        self.assertEqual(len(self.cluster.writes), 1)
        self.ledger.close()
        self.ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.adapters = replace(self.adapters, ledger=self.ledger)
        self.now = self.now.replace(second=41)
        result = self.run_worker()[0]
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_delayed_write_is_reconciled_through_coordinator_without_resubmission(self):
        class DelayedAPI(FakeDeploymentAPI):
            restore_calls = 0

            def conditional_restore(self, **kwargs):
                self.restore_calls += 1
                self.pending = deepcopy(kwargs)
                raise TimeoutError("request completion is unknown")

            def complete_pending(self):
                return super().conditional_restore(**self.pending)

        self.cluster = DelayedAPI(self.cluster.snapshot, clock=lambda: self.now)
        self.adapters = replace(self.adapters, cluster=self.cluster)
        self.prepare()
        self.assertEqual(self.run_worker()[0]["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")
        self.cluster.complete_pending()
        self.now = self.now.replace(second=41)
        result = self.run_worker()[0]
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.cluster.restore_calls, 1)
        self.assertEqual(len(self.cluster.writes), 1)

    def test_target_busy_leaves_the_durable_command_pending(self):
        from services.executor.store import TargetBusy

        self.prepare()
        with self.adapters.coordination.hold(self.proposal["target"]):
            with self.assertRaises(TargetBusy):
                self.run_worker()
        self.assertEqual(self.state(), "READY")
        pending = self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE topic='executor.request' AND delivered=0"
        ).fetchone()[0]
        self.assertEqual(pending, 1)
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.run_worker()[0]["status"], "APPLIED")

    def crash_after_write(self):
        class ProcessCrash(BaseException):
            pass

        class CrashingAPI(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                super().conditional_restore(**kwargs)
                raise ProcessCrash("write committed before process death")

        self.cluster = CrashingAPI(self.cluster.snapshot, clock=lambda: self.now)
        self.adapters = replace(self.adapters, cluster=self.cluster)
        self.prepare()
        with self.assertRaises(ProcessCrash):
            self.run_worker()
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "WRITE_STARTED")
        self.assertEqual(len(self.cluster.writes), 1)

    def revoke_pending_execution(self, verdict):
        self.now = self.now.replace(second=41)
        decision = deepcopy(self.store.action(self.proposal["action_id"])["guard_result"])
        decision.update(verdict=verdict, checked_at="2026-10-10T12:00:41Z",
                        reason_codes=["TARGET_NOT_ALLOWED" if verdict == "REJECT" else "APPROVAL_EXPIRED"])
        handle(decision, self.store, self.bus)
        self.assertEqual(self.store.db.execute(
            "SELECT count(*) FROM outbox WHERE topic='executor.request' AND delivered=0"
        ).fetchone()[0], 0)

    def assert_cancelled_write_is_reconciled(self, expected_state):
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.state(), expected_state)
        self.assertIsNone(self.store.action(self.proposal["action_id"])["execution_result"])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")
        self.assertEqual(self.ledger.pending_proposals(), [])
        self.assertEqual(len(self.cluster.write_attempts), 1)
        review = execution_delivery_reviews(self.store)[0]
        self.assertEqual(review["event_id"], 0)
        self.assertEqual(review["result"]["status"], "APPLIED")
        self.assertEqual(review["coordinator_state"], expected_state)

    def test_rejection_after_crash_reconciles_even_a_cancelled_execution_command(self):
        self.crash_after_write()
        self.revoke_pending_execution("REJECT")
        self.assert_cancelled_write_is_reconciled("REJECTED")
        # The confirmed write no longer excludes a later action on this workload.
        later = dict(self.proposal, action_id="ACT-LATER")
        self.assertEqual(self.ledger.prepare(later)["phase"], "PREPARED")

    def test_escalation_after_crash_reconciles_without_overriding_approval_state(self):
        self.crash_after_write()
        self.revoke_pending_execution("ESCALATE")
        self.assert_cancelled_write_is_reconciled("AWAITING_APPROVAL")

    def test_completed_ledger_result_is_reviewed_if_later_rejection_cancels_delivery(self):
        self.prepare()
        with patch("runtime.executor_pipeline.record_message", side_effect=RuntimeError("commit interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_worker()
        saved = deepcopy(self.ledger.get(self.proposal["action_id"])["result"])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")
        self.revoke_pending_execution("REJECT")
        self.assert_cancelled_write_is_reconciled("REJECTED")
        self.assertEqual(execution_delivery_reviews(self.store)[0]["result"], saved)

    def test_newer_allow_preserves_saved_result_and_creates_durable_review(self):
        self.prepare()
        with patch("runtime.executor_pipeline.record_message", side_effect=RuntimeError("commit interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_worker()
        saved = deepcopy(self.ledger.get(self.proposal["action_id"])["result"])
        decision = deepcopy(self.store.action(self.proposal["action_id"])["guard_result"])
        # Simulate a later delivered ALLOW for the same proposal; the earlier
        # completed execution must not be relabelled as happening after it.
        decision["checked_at"] = "2026-10-10T12:00:41Z"
        handle(decision, self.store, self.bus)
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.state(), "READY")
        self.assertIsNone(self.store.action(self.proposal["action_id"])["execution_result"])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["result"], saved)
        self.assertEqual(len(self.cluster.write_attempts), 1)
        self.store.close()
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        review = execution_delivery_reviews(self.store)[0]
        self.assertEqual(review["result"], saved)
        self.assertEqual(review["guard_result"], decision)
        self.assertIn("predates", review["reason"])

    def test_review_persistence_failure_replays_completed_observation_after_restart(self):
        self.crash_after_write()
        self.revoke_pending_execution("REJECT")
        with patch("runtime.executor_pipeline._record_review", side_effect=RuntimeError("review interrupted")):
            with self.assertRaisesRegex(RuntimeError, "review interrupted"):
                self.run_worker()
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")
        self.store.close()
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        self.assert_cancelled_write_is_reconciled("REJECTED")

    def test_read_only_scan_does_not_start_a_prepared_action(self):
        self.prepare()
        self.ledger.prepare(self.proposal)
        self.assertEqual(reconcile_pending_executions(self.adapters), [])
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "PREPARED")

    def test_coordinator_commit_before_ledger_ack_replays_without_a_review(self):
        self.prepare()
        with patch.object(self.ledger, "acknowledge_result", side_effect=RuntimeError("ledger ack interrupted")):
            with self.assertRaisesRegex(RuntimeError, "ledger ack interrupted"):
                self.run_worker()
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["delivered"], 0)
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["delivered"], 1)
        self.assertEqual(execution_delivery_reviews(self.store), [])
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_review_commit_before_ledger_ack_replays_the_same_review(self):
        self.crash_after_write()
        self.revoke_pending_execution("REJECT")
        with patch.object(self.ledger, "acknowledge_result", side_effect=RuntimeError("ledger ack interrupted")):
            with self.assertRaises(RuntimeError):
                self.run_worker()
        self.assertEqual(len(execution_delivery_reviews(self.store)), 1)
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["delivered"], 0)
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(len(execution_delivery_reviews(self.store)), 1)
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["delivered"], 1)
        self.assertEqual(self.state(), "REJECTED")
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_read_only_scan_skips_an_owned_target_then_reconciles_after_release(self):
        self.crash_after_write()
        self.revoke_pending_execution("REJECT")
        with self.adapters.coordination.hold(self.proposal["target"]):
            self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "WRITE_STARTED")
        self.assert_cancelled_write_is_reconciled("REJECTED")

    def test_revocation_during_api_call_keeps_applied_result_in_delivery_review(self):
        original_restore = self.cluster.conditional_restore

        def restore_then_revoke(**kwargs):
            response = original_restore(**kwargs)
            self.revoke_pending_execution("REJECT")
            return response

        self.cluster.conditional_restore = restore_then_revoke
        self.prepare()
        self.assertEqual(self.run_worker(), [])
        self.assertEqual(self.state(), "REJECTED")
        review = execution_delivery_reviews(self.store)[0]
        self.assertEqual(review["result"]["status"], "APPLIED")
        self.assertEqual(review["coordinator_state"], "REJECTED")
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["delivered"], 1)
        self.assertEqual(len(self.cluster.write_attempts), 1)


if __name__ == "__main__":
    unittest.main()
