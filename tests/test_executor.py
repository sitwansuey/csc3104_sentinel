"""Offline executor safety checks using SQLite, real Guard and a fake API.

These tests assert object writes and durable restart behavior, not service health
or live Kubernetes recovery. All approvals and baseline records are fixtures.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from runtime.executor_pipeline import GuardAuthorizer
from scripts.demo_guard import load_fixtures
from services.executor.core import (
    ACTION_ANNOTATION, ConditionalWriteConflict, ExecutionAdapters,
    ReadUnavailable, execute,
)
from tests.support.executor_fake import FakeDeploymentAPI
from services.executor.store import (
    ActionConflict, LocalTargetCoordinator, SQLiteExecutionLedger, TargetBusy,
)
from services.guard.core import proposal_fingerprint
from shared.contracts import validate_message


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


class SimulatedCrash(BaseException):
    """Skip ordinary exception handling to model process interruption."""


class CrashBeforeCommit(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        raise SimulatedCrash()


class CrashAfterCommit(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        super().conditional_restore(**kwargs)
        raise SimulatedCrash()


class LostResponse(FakeDeploymentAPI):
    def conditional_restore(self, **kwargs):
        super().conditional_restore(**kwargs)
        raise TimeoutError("Response lost after commit")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "executor.db"
        self.fixtures = load_fixtures()
        self.proposal = self.fixtures["proposal"]
        self.approval = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.fixtures["policy"]["policy_version"],
            "operator_id": "fabricated-test-operator", "decision": "APPROVED",
            "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        }
        self.approval_reads = 0
        self.authorizer = GuardAuthorizer(
            policy_reader=lambda p: deepcopy(self.fixtures["policy"]),
            baseline_reader=lambda p: deepcopy(self.fixtures["baseline"]),
            dependency_reader=lambda p: deepcopy(self.fixtures["dependencies"]),
            approval_reader=self.read_approval,
        )
        self.cluster = FakeDeploymentAPI(self.fixtures["live_state"], clock=lambda: NOW)
        self.coordination = LocalTargetCoordinator(self.directory / "locks")
        self.ledger = self.open_ledger()

    def read_approval(self, proposal):
        self.approval_reads += 1
        return deepcopy(self.approval)

    def open_ledger(self):
        ledger = SQLiteExecutionLedger(self.path)
        self.addCleanup(ledger.close)
        return ledger

    def adapters(self, **changes):
        values = dict(ledger=self.ledger, coordination=self.coordination,
                      cluster=self.cluster, authorizer=self.authorizer,
                      clock=lambda: NOW)
        values.update(changes)
        return ExecutionAdapters(**values)

    def run_action(self, **changes):
        return execute(deepcopy(self.proposal), self.adapters(**changes))

    def restart(self, **changes):
        self.ledger.close()
        self.ledger = self.open_ledger()
        return self.run_action(**changes)

    def assert_no_writes(self):
        self.assertEqual(self.cluster.write_attempts, [])
        self.assertEqual(self.cluster.writes, [])

    def test_approved_rollback_restores_exact_baseline_and_records_attempt(self):
        result = self.run_action()
        validate_message(result)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"],
                         self.fixtures["baseline"]["pod_template"])
        self.assertIn(ACTION_ANNOTATION, self.cluster.snapshot["deployment"]["metadata"]["annotations"])
        entry = self.ledger.get(self.proposal["action_id"])
        self.assertEqual(entry["proposal"], self.proposal)
        self.assertEqual(entry["fingerprint"], proposal_fingerprint(self.proposal))
        self.assertEqual(entry["result"], result)
        self.assertEqual(len(self.ledger.attempts(self.proposal["action_id"])), 1)
        self.assertEqual(self.approval_reads, 1)

    def test_missing_expired_and_rejected_approval_produce_no_write(self):
        for case in ("missing", "expired", "rejected"):
            with self.subTest(case=case):
                proposal = deepcopy(self.proposal)
                proposal["action_id"] = "approval-" + case
                self.fixtures["dependencies"]["action_id"] = proposal["action_id"]
                approval = deepcopy(self.approval)
                approval["action_id"] = proposal["action_id"]
                approval["proposal_sha256"] = proposal_fingerprint(proposal)
                if case == "missing":
                    approval = None
                elif case == "expired":
                    approval["expires_at"] = NOW.isoformat()
                else:
                    approval["decision"] = "REJECTED"
                self.approval = approval
                result = execute(proposal, self.adapters())
                self.assertEqual(result["status"], "FAILED")
                self.assert_no_writes()
                # Restore a valid record before constructing the next subcase.
                self.approval = {
                    "action_id": self.proposal["action_id"],
                    "proposal_sha256": proposal_fingerprint(self.proposal),
                    "policy_version": self.fixtures["policy"]["policy_version"],
                    "operator_id": "fabricated-test-operator", "decision": "APPROVED",
                    "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
                    "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                }

    def test_approval_is_looked_up_fresh_instead_of_reusing_old_allow(self):
        checked = self.authorizer.validate(self.proposal, self.cluster.read(self.proposal["target"]), now=NOW)
        self.assertEqual(checked.decision["verdict"], "ALLOW")
        self.approval = None
        self.assertEqual(self.run_action()["status"], "FAILED")
        self.assertEqual(self.approval_reads, 2)
        self.assert_no_writes()

    def test_operator_approval_cannot_override_hard_guard_rejection(self):
        self.fixtures["policy"]["targets"] = []
        result = self.run_action()
        self.assertEqual(result["status"], "FAILED")
        self.assert_no_writes()

    def test_tampered_baseline_fails_without_a_write(self):
        self.fixtures["baseline"]["pod_template"]["spec"]["containers"][0]["image"] = "tampered:latest"
        self.assertEqual(self.run_action()["status"], "FAILED")
        self.assert_no_writes()

    def test_unavailable_authoritative_approval_lookup_fails_closed(self):
        def unavailable(proposal):
            raise RuntimeError("Approval store unavailable")
        self.authorizer.approval_reader = unavailable
        self.assertEqual(self.run_action()["status"], "FAILED")
        self.assert_no_writes()

    def test_duplicate_delivery_returns_identical_result_after_restart(self):
        original = self.run_action()
        self.approval = None
        repeated = self.restart()
        self.assertEqual(repeated, original)
        self.assertEqual(len(self.cluster.write_attempts), 1)
        self.assertEqual(self.approval_reads, 1)

    def test_changed_intent_under_same_id_is_rejected_including_changed_target(self):
        self.run_action()
        for case in ("rationale", "target"):
            changed = deepcopy(self.proposal)
            if case == "rationale":
                changed["rationale"] = "Different approved intent"
            else:
                changed["target"]["name"] = "another-deployment"
            with self.subTest(case=case), self.assertRaises(ActionConflict):
                execute(changed, self.adapters())
        self.assertEqual(len(self.cluster.writes), 1)

    def test_changed_resource_version_or_recreated_uid_rejects_before_write(self):
        for field in ("resourceVersion", "uid"):
            with self.subTest(field=field):
                proposal = deepcopy(self.proposal)
                proposal["action_id"] = "stale-" + field
                self.fixtures["dependencies"]["action_id"] = proposal["action_id"]
                self.cluster.snapshot = deepcopy(self.fixtures["live_state"])
                self.cluster.snapshot["deployment"]["metadata"][field] = "changed"
                result = execute(proposal, self.adapters())
                self.assertEqual(result["status"], "CONFLICT")
                self.assert_no_writes()

    def test_conditional_write_catches_change_after_guard_allow(self):
        class ChangedBeforeWrite(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                api.snapshot["deployment"]["metadata"]["resourceVersion"] = "external-change"
                return super().conditional_restore(**kwargs)
        cluster = ChangedBeforeWrite(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "CONFLICT")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(cluster.writes, [])

    def test_busy_target_is_retryable_and_does_not_finalize_action(self):
        with self.coordination.hold(self.proposal["target"]):
            with self.assertRaises(TargetBusy):
                self.run_action()
        entry = self.ledger.get(self.proposal["action_id"])
        self.assertTrue(entry is None or entry["result"] is None)
        self.assert_no_writes()
        self.assertEqual(self.run_action()["status"], "APPLIED")

    def test_competing_workers_cannot_both_mutate_the_same_target(self):
        entered, release = Event(), Event()
        results, errors = [], []
        real_authorizer = self.authorizer

        class BlockingAuthorizer:
            def validate(authorizer, *args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("Test worker was not released")
                return real_authorizer.validate(*args, **kwargs)

            def assert_usable(authorizer, *args, **kwargs):
                return real_authorizer.assert_usable(*args, **kwargs)

        def first_worker():
            ledger = SQLiteExecutionLedger(self.path)
            try:
                results.append(self.run_action(ledger=ledger, authorizer=BlockingAuthorizer()))
            except BaseException as error:
                errors.append(error)
            finally:
                ledger.close()

        thread = Thread(target=first_worker)
        thread.start()
        try:
            self.assertTrue(entered.wait(10), "First worker failed to enter validation")
            rival = deepcopy(self.proposal)
            rival["action_id"] = "rival-action"
            with self.assertRaises(TargetBusy):
                execute(rival, self.adapters())
            self.assert_no_writes()
        finally:
            release.set()
            thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual([result["status"] for result in results], ["APPLIED"])
        self.assertEqual(len(self.cluster.writes), 1)

    def test_local_lock_excludes_another_process_and_is_released_on_abrupt_exit(self):
        child = """
import json, os, sys
from services.executor.store import LocalTargetCoordinator, TargetBusy
coordinator = LocalTargetCoordinator(sys.argv[1])
try:
    with coordinator.hold(json.loads(sys.argv[2])):
        os._exit(7)
except TargetBusy:
    sys.exit(8)
"""
        command = [sys.executable, "-c", child, str(self.directory / "locks"),
                   json.dumps(self.proposal["target"])]
        options = dict(cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=15)
        with self.coordination.hold(self.proposal["target"]):
            self.assertEqual(subprocess.run(command, **options).returncode, 8)
        # _exit skips context-manager cleanup: only the OS can release this lock.
        self.assertEqual(subprocess.run(command, **options).returncode, 7)
        with self.coordination.hold(self.proposal["target"]):
            pass

    def test_crash_before_write_intent_can_retry_after_restart(self):
        class CrashDuringRead(FakeDeploymentAPI):
            def read(api, target):
                raise SimulatedCrash()
        broken = CrashDuringRead(self.fixtures["live_state"])
        with self.assertRaises(SimulatedCrash):
            self.run_action(cluster=broken)
        self.assertEqual(self.restart()["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)

    def test_crash_after_write_intent_but_before_commit_does_not_repeat_mutation(self):
        cluster = CrashBeforeCommit(self.fixtures["live_state"], clock=lambda: NOW)
        with self.assertRaises(SimulatedCrash):
            self.run_action(cluster=cluster)
        entry = self.ledger.get(self.proposal["action_id"])
        self.assertEqual(entry["phase"], "WRITE_STARTED")
        self.assertEqual(len(self.ledger.attempts(self.proposal["action_id"])), 1)
        result = self.restart(cluster=cluster)
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(cluster.writes, [])

    def test_crash_after_commit_reconciles_after_restart_without_new_authorization(self):
        cluster = CrashAfterCommit(self.fixtures["live_state"], clock=lambda: NOW)
        with self.assertRaises(SimulatedCrash):
            self.run_action(cluster=cluster)
        self.approval = None
        self.fixtures["baseline"] = None
        self.fixtures["dependencies"] = None
        result = self.restart(cluster=cluster)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)
        self.assertEqual(self.approval_reads, 1)

    def test_write_intent_persistence_failure_prevents_the_api_call(self):
        failure = sqlite3.OperationalError("Synthetic journal write failure")
        with patch.object(self.ledger, "start_write", side_effect=failure):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_action()
        self.assert_no_writes()
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "PREPARED")
        self.assertEqual(self.restart()["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)

    def test_result_persistence_failure_after_commit_reconciles_on_restart(self):
        failure = sqlite3.OperationalError("Synthetic result write failure")
        with patch.object(self.ledger, "finish", side_effect=failure):
            with self.assertRaises(sqlite3.OperationalError):
                self.run_action()
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "WRITE_STARTED")
        self.assertEqual(len(self.cluster.writes), 1)
        self.approval = None
        self.assertEqual(self.restart()["status"], "APPLIED")
        self.assertEqual(len(self.cluster.write_attempts), 1)
        self.assertEqual(self.approval_reads, 1)

    def test_lost_write_response_reconciles_evidence_without_a_second_write(self):
        cluster = LostResponse(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_unknown_timeout_without_evidence_stays_ambiguous_even_with_original_version(self):
        class UnknownTimeout(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                raise TimeoutError("Request may still be in flight")
        cluster = UnknownTimeout(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(cluster.writes, [])
        self.assertEqual(cluster.snapshot["deployment"]["metadata"]["resourceVersion"],
                         self.proposal["based_on_resource_version"])

    def test_delayed_write_reconciles_once_and_old_ack_cannot_hide_the_new_result(self):
        class DelayedWrite(FakeDeploymentAPI):
            requests = 0

            def conditional_restore(api, **kwargs):
                api.requests += 1
                api.pending = deepcopy(kwargs)
                raise TimeoutError("Server still processing original request")
        cluster = DelayedWrite(self.fixtures["live_state"], clock=lambda: NOW)
        uncertain = self.run_action(cluster=cluster)
        self.assertEqual(uncertain["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(self.ledger.undelivered_results(), [uncertain])
        # The server finishes the already submitted request after the timeout.
        FakeDeploymentAPI.conditional_restore(cluster, **cluster.pending)
        self.approval = None
        applied = self.restart(cluster=cluster)
        self.assertEqual(applied["status"], "APPLIED")
        # An older worker may finish its Coordinator delivery after reconciliation.
        # Its acknowledgment must leave the newer APPLIED result pending delivery.
        self.assertFalse(self.ledger.acknowledge_result(uncertain))
        self.assertEqual(self.ledger.undelivered_results(), [applied])
        self.assertTrue(self.ledger.acknowledge_result(applied))
        self.assertEqual(self.ledger.undelivered_results(), [])
        self.assertEqual(cluster.requests, 1)
        self.assertEqual(len(cluster.writes), 1)
        self.assertEqual(self.approval_reads, 1)
        self.assertEqual([result["status"] for result in self.ledger.results(self.proposal["action_id"])],
                         ["NEEDS_RECONCILIATION", "APPLIED"])

    def test_authorization_expiring_during_journal_commit_prevents_the_api_call(self):
        current = [NOW]
        start_write = self.ledger.start_write

        def delayed_journal(*args, **kwargs):
            start_write(*args, **kwargs)
            current[0] = NOW + timedelta(seconds=30)

        with patch.object(self.ledger, "start_write", side_effect=delayed_journal):
            result = self.run_action(clock=lambda: current[0])
        self.assertEqual(result["status"], "FAILED")
        self.assert_no_writes()
        self.assertEqual(len(self.ledger.attempts(self.proposal["action_id"])), 1)
        self.assertEqual(self.ledger.get(self.proposal["action_id"])["phase"], "DONE")

    def test_guard_input_binding_is_rechecked_after_journal_commit(self):
        checks = []
        validate = self.authorizer.validate
        start_write = self.ledger.start_write

        def capture_check(*args, **kwargs):
            check = validate(*args, **kwargs)
            checks.append(check)
            return check

        def changed_input(*args, **kwargs):
            start_write(*args, **kwargs)
            checks[0].inputs["baseline"]["pod_template"]["spec"]["containers"][0]["image"] = "tampered:latest"

        with patch.object(self.authorizer, "validate", side_effect=capture_check):
            with patch.object(self.ledger, "start_write", side_effect=changed_input):
                self.assertEqual(self.run_action()["status"], "FAILED")
        self.assert_no_writes()

    def test_malformed_success_response_requires_a_fresh_read_without_repeating_mutation(self):
        class MalformedResponse(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                response = super().conditional_restore(**kwargs)
                response["deployment"]["metadata"]["annotations"] = ["invalid"]
                return response
        cluster = MalformedResponse(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_malformed_success_response_and_unavailable_read_remain_uncertain(self):
        class MalformedResponseAndReadOutage(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                response = super().conditional_restore(**kwargs)
                response["deployment"]["metadata"]["annotations"] = ["invalid"]
                return response

            def read(api, target):
                if api.writes:
                    raise ReadUnavailable("Read unavailable after commit")
                return super().read(target)
        cluster = MalformedResponseAndReadOutage(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_reconciliation_requires_exact_evidence_template_and_target_identity(self):
        for case in ("absent", "malformed", "action-id-only", "wrong-template", "wrong-uid"):
            with self.subTest(case=case):
                ledger = SQLiteExecutionLedger(self.directory / (case + ".db"))
                self.addCleanup(ledger.close)
                proposal = deepcopy(self.proposal)
                proposal["action_id"] = "reconcile-" + case
                self.fixtures["dependencies"]["action_id"] = proposal["action_id"]
                self.approval["action_id"] = proposal["action_id"]
                self.approval["proposal_sha256"] = proposal_fingerprint(proposal)
                cluster = CrashAfterCommit(self.fixtures["live_state"], clock=lambda: NOW)
                with self.assertRaises(SimulatedCrash):
                    execute(proposal, self.adapters(cluster=cluster, ledger=ledger))
                metadata = cluster.snapshot["deployment"]["metadata"]
                if case == "absent":
                    del metadata["annotations"][ACTION_ANNOTATION]
                elif case == "malformed":
                    metadata["annotations"][ACTION_ANNOTATION] = "not json"
                elif case == "action-id-only":
                    metadata["annotations"][ACTION_ANNOTATION] = json.dumps({"action_id": proposal["action_id"]})
                elif case == "wrong-template":
                    cluster.snapshot["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = "external:change"
                else:
                    metadata["uid"] = "recreated-target"
                result = execute(proposal, self.adapters(cluster=cluster, ledger=ledger))
                self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
                self.assertEqual(len(cluster.write_attempts), 1)

    def test_uncertain_write_blocks_other_actions_even_after_worker_unlocks(self):
        class UnknownTimeout(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                raise TimeoutError("Request may still be in flight")
        cluster = UnknownTimeout(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster)["status"], "NEEDS_RECONCILIATION")
        rival = deepcopy(self.proposal)
        rival["action_id"] = "rival-after-timeout"
        with self.assertRaises(TargetBusy):
            execute(rival, self.adapters())
        # A recreated object retains the workload coordination key.
        rival["target"]["uid"] = "recreated-uid"
        with self.assertRaises(TargetBusy):
            execute(rival, self.adapters())
        self.assert_no_writes()

    def test_resumed_prepared_action_cannot_bypass_an_uncertain_competitor(self):
        class CrashDuringRead(FakeDeploymentAPI):
            def read(api, target):
                raise SimulatedCrash()
        rival = deepcopy(self.proposal)
        rival["action_id"] = "prepared-rival"
        with self.assertRaises(SimulatedCrash):
            execute(rival, self.adapters(cluster=CrashDuringRead(self.fixtures["live_state"])))
        uncertain = CrashBeforeCommit(self.fixtures["live_state"], clock=lambda: NOW)
        with self.assertRaises(SimulatedCrash):
            self.run_action(cluster=uncertain)
        with self.assertRaises(TargetBusy):
            execute(rival, self.adapters())
        self.assert_no_writes()

    def test_initial_reads_retry_only_up_to_configured_bound(self):
        class UnavailableReads(FakeDeploymentAPI):
            calls = 0

            def read(api, target):
                api.calls += 1
                raise ReadUnavailable("Synthetic read outage")
        cluster = UnavailableReads(self.fixtures["live_state"])
        result = self.run_action(cluster=cluster, read_attempts=2)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(cluster.calls, 2)
        self.assertEqual(cluster.write_attempts, [])

    def test_temporary_read_outage_can_recover_before_any_write(self):
        class TemporaryReadOutage(FakeDeploymentAPI):
            calls = 0

            def read(api, target):
                api.calls += 1
                if api.calls == 1:
                    raise ReadUnavailable("Temporary outage")
                return super().read(target)
        cluster = TemporaryReadOutage(self.fixtures["live_state"], clock=lambda: NOW)
        self.assertEqual(self.run_action(cluster=cluster, read_attempts=2)["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_reconciliation_read_outage_is_bounded_without_repeating_write(self):
        class LostResponseAndReads(FakeDeploymentAPI):
            reconciliation_reads = 0

            def read(api, target):
                if api.writes:
                    api.reconciliation_reads += 1
                    raise ReadUnavailable("Read unavailable after write")
                return super().read(target)

            def conditional_restore(api, **kwargs):
                super().conditional_restore(**kwargs)
                raise TimeoutError("Response lost")
        cluster = LostResponseAndReads(self.fixtures["live_state"], clock=lambda: NOW)
        result = self.run_action(cluster=cluster, read_attempts=2)
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertEqual(cluster.reconciliation_reads, 2)
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_fake_api_rejects_cas_mismatch_and_preserves_unrelated_annotations(self):
        self.cluster.snapshot["deployment"]["metadata"]["annotations"] = {"example.org/owner": "team"}
        with self.assertRaises(ConditionalWriteConflict):
            self.cluster.conditional_restore(
                target=self.proposal["target"], version="stale",
                template=self.fixtures["baseline"]["pod_template"], evidence={},
            )
        self.assertEqual(self.cluster.writes, [])
        self.assertEqual(self.run_action()["status"], "APPLIED")
        self.assertEqual(self.cluster.snapshot["deployment"]["metadata"]["annotations"]["example.org/owner"], "team")


if __name__ == "__main__":
    unittest.main()
