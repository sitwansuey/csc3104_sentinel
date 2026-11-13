import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest

from services.coordinator.core import (
    CoordinatorError, LocalEventBus, SQLiteStore, acknowledge, approve,
    dispatch_pending, handle,
)
from experiments.boutique.harness.multi_fault import cross_incident_transitions
from services.guard.core import evaluate, proposal_fingerprint

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 36, tzinfo=timezone.utc)


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        self.incident = fixture("messages/incident.json")
        self.proposal = fixture("messages/proposal.json")
        self.guard = fixture("messages/guard-result.json")
        self.execution = fixture("messages/execution-result.json")
        self.outcome = fixture("messages/outcome.json")
        self.iid, self.aid = self.incident["incident_id"], self.proposal["action_id"]

    def send(self, message):
        return handle(message, self.store, self.bus)

    def proposed(self):
        self.send(self.incident)
        self.send(self.proposal)

    def allowed(self):
        self.proposed()
        self.guard["verdict"] = "ALLOW"
        self.guard["reason_codes"] = ["VALIDATION_PASSED"]
        self.send(self.guard)

    def state(self):
        return self.store.incident(self.iid)["state"]

    def test_incident_is_durable_and_routes_once_in_memory(self):
        self.assertTrue(self.send(self.incident))
        self.assertFalse(self.send(copy.deepcopy(self.incident)))
        self.assertEqual(self.state(), "OPEN")
        self.assertEqual(len(self.bus.events), 1)
        self.assertEqual(self.bus.events[0]["topic"], "planner.request")

    def test_changed_payload_under_stable_id_is_rejected(self):
        self.proposed()
        self.proposal["rationale"] = "changed"
        with self.assertRaises(CoordinatorError):
            self.send(self.proposal)
        self.assertNotEqual(self.store.action(self.aid)["proposal"]["rationale"], "changed")

    def test_out_of_order_message_rolls_back_without_recording(self):
        with self.assertRaises(CoordinatorError):
            self.send(self.proposal)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
        self.assertEqual(self.bus.events, [])
        self.send(self.incident)
        self.assertTrue(self.send(self.proposal))

    def test_target_and_action_correlation_are_enforced(self):
        self.proposed()
        for field, value in (("target", dict(self.guard["target"], uid="other")),
                             ("action_id", "other"), ("incident_id", "other")):
            with self.subTest(field=field), self.assertRaises(CoordinatorError):
                self.send(dict(self.guard, **{field: value}))

    def test_only_one_active_proposal(self):
        self.proposed()
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.proposal, action_id="ACT-OTHER"))

    def test_approval_matches_real_guard_and_requires_revalidation(self):
        self.proposed()
        self.send(self.guard)
        record = approve(self.aid, "operator-1", self.store)
        self.assertEqual(record["proposal_sha256"], proposal_fingerprint(self.proposal))
        self.assertEqual(self.store.approval(self.aid), record)
        self.assertEqual(self.state(), "VALIDATING")
        self.assertFalse(any(e["topic"] == "executor.request" for e in self.bus.events))
        result = evaluate(self.proposal, fixture("observations.json")["failed-rollout"][-1],
                          fixture("policy.json"), fixture("baseline.json"),
                          fixture("messages/dependency-result.json"), record,
                          now=datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc))
        self.assertEqual(result["verdict"], "ALLOW")
        self.send(result)
        self.assertEqual(self.state(), "READY")

    def test_rejection_cannot_be_overridden_by_approval_or_later_allow(self):
        self.proposed()
        self.send(dict(self.guard, verdict="REJECT", reason_codes=["TARGET_NOT_ALLOWED"]))
        with self.assertRaises(CoordinatorError):
            approve(self.aid, "operator", self.store)
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.guard, verdict="ALLOW", checked_at="2026-10-10T12:00:40Z"))
        self.assertEqual(self.state(), "REJECTED")

    def test_approval_retry_reuses_record_and_does_not_queue_twice(self):
        self.proposed()
        self.send(self.guard)
        first = approve(self.aid, "operator", self.store)
        count = self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(approve(self.aid, "operator", self.store), first)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM approvals").fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM outbox").fetchone()[0], count)

    def test_revoked_allow_cancels_queued_execution(self):
        self.allowed()
        self.assertTrue(any(e["topic"] == "executor.request" for e in self.bus.events))
        self.send(dict(self.guard, verdict="REJECT", checked_at="2026-10-10T12:00:40Z"))
        self.assertFalse(any(e["topic"] == "executor.request" for e in self.bus.events))
        self.assertEqual(dispatch_pending(self.store, LocalEventBus()), 0)

    def test_execution_timestamp_and_resource_version_are_required(self):
        self.allowed()
        for changes in ({"recorded_at": "2026-10-10T12:00:00Z"}, {"resource_version": None}):
            with self.subTest(changes=changes), self.assertRaises(CoordinatorError):
                self.send(dict(self.execution, **changes))
        self.assertEqual(self.state(), "READY")

    def test_unknown_dependencies_cannot_be_approved(self):
        self.proposed()
        self.send(dict(self.guard, reason_codes=["DEPENDENCY_UNKNOWN"]))
        with self.assertRaises(CoordinatorError):
            approve(self.aid, "operator", self.store)

    def test_approval_requires_identity_and_eligible_proposal(self):
        self.proposed()
        with self.assertRaises(CoordinatorError):
            approve(self.aid, "operator", self.store)
        self.send(self.guard)
        with self.assertRaises(CoordinatorError):
            approve(self.aid, " ", self.store)

    def test_old_guard_results_and_changed_timestamp_payloads_are_rejected(self):
        self.allowed()
        for stamp in ("2026-10-10T12:00:34Z", self.guard["checked_at"]):
            with self.subTest(stamp=stamp), self.assertRaises(CoordinatorError):
                self.send(dict(self.guard, checked_at=stamp, verdict="REJECT"))
        self.assertEqual(self.state(), "READY")

    def test_allow_for_another_resource_version_is_rejected(self):
        self.proposed()
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.guard, verdict="ALLOW", validated_resource_version="other"))

    def test_execution_without_allow_is_rejected(self):
        self.proposed()
        self.send(self.guard)
        with self.assertRaises(CoordinatorError):
            self.send(self.execution)

    def test_applied_is_not_recovered_until_verified(self):
        self.allowed()
        self.send(self.execution)
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.bus.events[-1]["topic"], "verifier.request")
        self.send(self.outcome)
        self.assertEqual(self.state(), "RECOVERED")
        self.assertFalse(self.send(self.outcome))
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.proposal, action_id="ACT-NEW"))

    def test_failed_and_unknown_health_are_not_recovered(self):
        for status, expected in (("NOT_RECOVERED", "FAILED"), ("UNKNOWN", "NEEDS_REVIEW")):
            with self.subTest(status=status), SQLiteStore() as store:
                for message in (self.incident, self.proposal,
                                dict(self.guard, verdict="ALLOW"), self.execution,
                                dict(self.outcome, status=status)):
                    handle(message, store, LocalEventBus())
                self.assertEqual(store.incident(self.iid)["state"], expected)

    def test_recovery_with_failed_check_or_pre_execution_window_is_rejected(self):
        self.allowed()
        self.send(self.execution)
        bad_checks = [dict(self.outcome["checks"][0], passed=False)]
        for changes in ({"checks": bad_checks}, {"window_start": "2026-10-10T12:00:00Z"}):
            with self.subTest(changes=changes), self.assertRaises(CoordinatorError):
                self.send(dict(self.outcome, **changes))
        self.assertEqual(self.state(), "VERIFYING")

    def test_outcome_before_execution_is_rejected(self):
        self.allowed()
        with self.assertRaises(CoordinatorError):
            self.send(self.outcome)

    def test_conflict_allows_new_action_but_rejects_late_old_results(self):
        self.allowed()
        self.send(dict(self.execution, status="CONFLICT"))
        self.assertEqual(self.state(), "FAILED")
        self.send(dict(self.proposal, action_id="ACT-NEW"))
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.execution, recorded_at="2026-10-10T12:00:46Z"))

    def test_superseded_write_fails_without_verification_and_admits_a_new_action(self):
        for first in ("READY", "NEEDS_RECONCILIATION"):
            with self.subTest(first=first):
                self.store = SQLiteStore(clock=lambda: NOW)
                self.addCleanup(self.store.close)
                self.bus = LocalEventBus()
                self.allowed()
                if first == "NEEDS_RECONCILIATION":
                    self.send(dict(self.execution, status="NEEDS_RECONCILIATION"))
                self.send(dict(self.execution, status="SUPERSEDED", recorded_at="2026-10-10T12:00:46Z"))
                self.assertEqual(self.state(), "FAILED")
                self.assertFalse(any(event["topic"] == "verifier.request" for event in self.bus.events))
                self.send(dict(self.proposal, action_id="ACT-NEW"))
                with self.assertRaises(CoordinatorError):
                    self.send(dict(self.execution, status="APPLIED", recorded_at="2026-10-10T12:00:47Z"))

    def test_uncertain_write_requires_reconciliation_before_new_action(self):
        self.allowed()
        self.send(dict(self.execution, status="NEEDS_RECONCILIATION"))
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.proposal, action_id="ACT-NEW"))
        self.send(dict(self.execution, recorded_at="2026-10-10T12:00:46Z"))
        self.assertEqual(self.state(), "VERIFYING")

    def test_dependency_result_routes_to_guard(self):
        self.proposed()
        self.send(fixture("messages/dependency-result.json"))
        self.assertIn("dependencies", self.bus.events[-1]["payload"])

    def test_restart_recovers_records_approvals_and_pending_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with SQLiteStore(path, clock=lambda: NOW) as first:
                for message in (self.incident, self.proposal, self.guard):
                    handle(message, first, LocalEventBus())
                record = approve(self.aid, "operator", first)
            with SQLiteStore(path) as second:
                self.assertEqual(second.approval(self.aid), record)
                self.assertEqual(second.incident(self.iid)["state"], "VALIDATING")
                bus = LocalEventBus()
                self.assertGreater(dispatch_pending(second, bus), 0)
                ids = [e["event_id"] for e in bus.events]
                for event_id in ids:
                    acknowledge(second, event_id)
                self.assertEqual(dispatch_pending(second, LocalEventBus()), 0)
                self.assertFalse(handle(self.incident, second, LocalEventBus()))

    def test_publish_failure_keeps_committed_event_for_retry(self):
        class BrokenBus:
            def publish(self, event):
                raise RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            handle(self.incident, self.store, BrokenBus())
        self.assertEqual(self.state(), "OPEN")
        self.assertFalse(self.send(self.incident))
        self.assertEqual(len(self.bus.events), 1)

    def test_local_bus_routes_and_retains_failed_delivery(self):
        self.send(self.incident)
        delivered = []
        def consumer(event):
            delivered.append(event["payload"]["incident"])
            acknowledge(self.store, event["event_id"])
        self.bus.subscribe("planner.request", consumer)
        self.bus.drain()
        self.assertEqual(delivered, [self.incident])
        self.assertEqual(dispatch_pending(self.store, self.bus), 0)

    def test_subscriber_failure_does_not_lose_command(self):
        self.send(self.incident)
        def fail(event):
            raise RuntimeError("consumer unavailable")
        self.bus.subscribe("planner.request", fail)
        with self.assertRaises(RuntimeError):
            self.bus.drain()
        self.assertEqual(len(self.bus.events), 1)
        self.assertEqual(dispatch_pending(self.store, LocalEventBus()), 1)

    def test_two_connections_cannot_admit_competing_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with SQLiteStore(path) as first, SQLiteStore(path) as second:
                handle(self.incident, first, LocalEventBus())
                handle(self.proposal, first, LocalEventBus())
                with self.assertRaises(CoordinatorError):
                    handle(dict(self.proposal, action_id="ACT-OTHER"), second, LocalEventBus())
                self.assertIsNone(second.action("ACT-OTHER"))

    def test_simultaneous_workers_admit_only_one_action(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "coordinator.db"
            with SQLiteStore(path) as store:
                handle(self.incident, store, LocalEventBus())
            barrier = threading.Barrier(2)
            results = []
            def worker(action_id):
                try:
                    with SQLiteStore(path) as store:
                        barrier.wait(timeout=5)
                        handle(dict(self.proposal, action_id=action_id), store, LocalEventBus())
                    results.append("accepted")
                except CoordinatorError:
                    results.append("rejected")
                except Exception as exc:
                    results.append(repr(exc))
            threads = [threading.Thread(target=worker, args=(f"ACT-{index}",)) for index in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertCountEqual(results, ["accepted", "rejected"])



def other_target(message):
    """The same lifecycle message for a second incident on another target (cartservice)."""
    text = json.dumps(message)
    for old, new in (("INC-DEMO-001", "INC-CART-001"), ("ACT-DEMO-001", "ACT-CART-001"),
                     ("synthetic-demo-uid-001", "cartservice-uid"), ("demo-api", "cartservice")):
        text = text.replace(old, new)
    return json.loads(text)


class CrossIncidentTests(unittest.TestCase):
    """Two incidents on two targets at once (P4.05): one incident's messages never move the other."""

    def setUp(self):
        self.store = SQLiteStore(clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        names = ("incident", "proposal", "guard-result", "execution-result", "outcome")
        self.a = {name: fixture("messages/" + name + ".json") for name in names}
        self.b = {name: other_target(message) for name, message in self.a.items()}
        for messages in (self.a, self.b):
            messages["guard-result"].update(verdict="ALLOW", reason_codes=["VALIDATION_PASSED"])

    def send(self, message):
        return handle(message, self.store, self.bus)

    def verifying(self):
        for name in ("incident", "proposal", "guard-result", "execution-result"):
            for messages in (self.a, self.b):
                self.send(messages[name])

    def state(self, messages):
        return self.store.incident(messages["incident"]["incident_id"])["state"]

    def test_an_outcome_for_one_incident_never_transitions_the_other(self):
        self.verifying()
        self.assertEqual((self.state(self.a), self.state(self.b)), ("VERIFYING", "VERIFYING"))
        before = len(self.store.transitions(self.b["incident"]["incident_id"]))
        self.send(self.a["outcome"])
        self.assertEqual((self.state(self.a), self.state(self.b)), ("RECOVERED", "VERIFYING"))
        self.assertEqual(len(self.store.transitions(self.b["incident"]["incident_id"])), before)
        # A's outcome cannot be pointed at B's action, nor B's outcome at A's incident.
        for forged in (dict(self.a["outcome"], action_id=self.b["outcome"]["action_id"]),
                       dict(self.b["outcome"], incident_id=self.a["incident"]["incident_id"])):
            with self.subTest(forged=forged["incident_id"]), self.assertRaises(CoordinatorError):
                self.send(forged)
        self.assertEqual((self.state(self.a), self.state(self.b)), ("RECOVERED", "VERIFYING"))
        self.send(dict(self.b["outcome"], status="NOT_RECOVERED"))
        self.assertEqual((self.state(self.a), self.state(self.b)), ("RECOVERED", "FAILED"))
        self.assertEqual(cross_incident_transitions(self.store), [])

    def test_rejected_admits_a_new_proposal_but_recovered_never_reopens(self):
        self.send(self.a["incident"])
        self.send(self.b["incident"])
        self.send(self.a["proposal"])
        self.send(self.b["proposal"])
        self.send(dict(self.b["guard-result"], verdict="REJECT", reason_codes=["DEPENDENCY_BLOCKED"]))
        self.assertEqual((self.state(self.a), self.state(self.b)), ("VALIDATING", "REJECTED"))
        # A rejected incident may be planned again: a new action ID, the same incident.
        self.send(dict(self.b["proposal"], action_id="ACT-CART-002", rationale="Re-planned after the blocker recovered."))
        self.assertEqual(self.state(self.b), "VALIDATING")
        self.assertEqual(self.store.incident(self.b["incident"]["incident_id"])["active_action_id"], "ACT-CART-002")
        for name in ("guard-result", "execution-result", "outcome"):
            self.send(self.a[name])
        self.assertEqual(self.state(self.a), "RECOVERED")
        with self.assertRaises(CoordinatorError):
            self.send(dict(self.a["proposal"], action_id="ACT-DEMO-002"))
        self.assertEqual(self.state(self.a), "RECOVERED")
        self.assertEqual(self.state(self.b), "VALIDATING")
        self.assertEqual(cross_incident_transitions(self.store), [])

    def test_the_cross_incident_check_reports_a_transition_caused_by_the_other_incident(self):
        self.verifying()
        self.send(self.a["outcome"])
        caused_by_a = self.store.transitions(self.a["incident"]["incident_id"])[-1]["cause"]
        # A forged journal row: B moved by A's outcome. The check must name it.
        self.store.db.execute(
            "INSERT INTO transitions(incident_id,action_id,from_state,to_state,cause,outbox_high_water,recorded_at)"
            " VALUES(?,?,?,?,?,?,?)", (self.b["incident"]["incident_id"], self.b["proposal"]["action_id"],
                                       "VERIFYING", "RECOVERED", caused_by_a, 0, "2026-10-10T12:00:36Z"))
        found = cross_incident_transitions(self.store)
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["incident_id"], found[0]["cause_incident_id"]),
                         (self.b["incident"]["incident_id"], self.a["incident"]["incident_id"]))


if __name__ == "__main__":
    unittest.main()
