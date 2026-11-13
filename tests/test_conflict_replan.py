"""A status write between the Guard's ALLOW and the Executor's patch (sentinel-dist, 13 Nov 2026).

In S0 r2 and r3 of the third sentinel-dist deployment (16:38-16:45Z) the
operator approved, the Guard allowed, and then the Deployment controller wrote
demo-api's status (its Recreate rollout and progress condition) before the
Executor's conditional patch. The Executor refused, correctly, and wrote
nothing: in r3 its own read already showed another resourceVersion ('Target
identity or resource version changed'), in r2 the adapter refused the patch's
precondition ('Conditional write was refused without committing'). CONFLICT
ended the incident FAILED, so nothing was repaired.

With the sentinel-dist setting (--stale-replans) such a CONFLICT now gets the
stale re-plan's path (services/coordinator/core.py::replan_after_conflict):
the Executor's result records why nothing committed and what it read, and when
that read shows the incident's generation, the Coordinator reopens the
incident for a new proposal bound to the version read, which the Guard checks
again and the operator approves again. A CONFLICT without that record, after a
spec change, after an earlier result, a revocation, a second approval or
ALLOW, a seal, for a RESTART, or past the budget stays final. At b9e4ddb the
two symptom tests end FAILED with no write.
"""
from dataclasses import replace
import json
import tempfile
import unittest

from experiments.distributed import invariants
from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.remote_adapters import DeterministicProvider
from runtime.remote_worker import RemoteIngress
from runtime.worker_http import WorkerClient
from services.coordinator.core import (
    CONFLICT_REPLAN_CAUSE, STALE_REPLAN_CAUSE, record_message, replan_after_conflict,
)
from services.executor.core import execute
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from services.planner.worker import REPLAN_NOTES, ReplanProvider, planning_request, prepare, replan_cause, replan_key
from shared.contracts import ContractError, validate_message
from tests.test_stale_replan import StaleReplanFlow, guard_result

CONFLICT_NOTE = "after the Executor wrote nothing for the previous proposal"


class ConflictReplanFlow(StaleReplanFlow):
    """StaleReplanFlow's loopback workers; the version moves after the ALLOW instead of before it."""

    def observe(self):
        RemoteIngress(WorkerClient(self.url, "observer", self.services["observer"][1], timeout=2))(self.incident)

    def allowed(self):
        """Plan, escalate, approve and ALLOW the active proposal; return it."""
        proposal = self.plan() if self.state() == "OPEN" else self.active()["proposal"]
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.approve(proposal)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.assertEqual(self.state(), "READY")
        return proposal

    def write_races_the_patch(self, change=None):
        """The controller's write lands after the Executor's fresh Guard check, just before its patch."""
        cluster, original = self.f.cluster, self.f.cluster.conditional_restore

        def racing(**request):
            cluster.conditional_restore = original
            cluster.external_write(change)
            return original(**request)
        cluster.conditional_restore = racing

    def conflict(self):
        """Run the Executor once and return the action's CONFLICT result."""
        before = self.active()
        self.assertEqual(self.executor.once()["status"], "accepted")
        result = self.f.store.action(before["action_id"])["execution_result"]
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.f.store.action(before["action_id"])["state"], "FAILED")
        return result

    def assert_conflict_final(self, refused):
        self.assertEqual(self.state(), "FAILED")
        self.assertEqual(self.active()["action_id"], refused["action_id"])
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], CONFLICT_REPLAN_CAUSE), 0)
        self.assertEqual(self.planner.once()["status"], "idle")
        self.assertEqual(self.f.cluster.writes, [])

    def assert_repaired_by_replan(self, refused, result, attempt=1):
        self.assertEqual(self.state(), "OPEN")  # Reopened for the re-plan, never left FAILED and silent.
        second = self.plan()
        self.assertNotEqual(second["action_id"], refused["action_id"])
        self.assertEqual(second["based_on_resource_version"], result["conflict"]["resource_version"])
        self.assertEqual(second["based_on_resource_version"], self.live_version())
        self.assertEqual({key: second[key] for key in ("action", "parameters", "target", "evidence_refs")},
                         {key: refused[key] for key in ("action", "parameters", "target", "evidence_refs")})
        self.assertIn(f"Re-planned (attempt {attempt}) " + CONFLICT_NOTE, second["rationale"])
        # The approval was bound to the first proposal's digest: the re-plan waits for its own.
        self.assertEqual(self.guard_pass()["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.assertEqual(self.f.cluster.writes, [])
        self.approve(second)
        self.assertEqual(self.guard_pass()["verdict"], "ALLOW")
        self.assertEqual(self.executor.once()["status"], "accepted")
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(len(self.f.cluster.writes), 1)
        old = self.f.store.action(refused["action_id"])
        self.assertEqual((old["state"], old["proposal"], old["execution_result"]), ("FAILED", refused, result))
        self.assertEqual(self.f.store.reopenings(self.incident["incident_id"], CONFLICT_REPLAN_CAUSE), 1)
        saved = self.planning.get(replan_key(self.incident["incident_id"], attempt))
        self.assertEqual((saved["proposal"], saved["delivered"]), (second, 1))
        self.assertTrue(invariants.x4(self.records())["passed"], invariants.x4(self.records())["detail"])
        return second


class SentinelDistConflictTests(ConflictReplanFlow):
    def test_r3_a_status_write_after_the_allow_is_replanned_and_repaired_with_one_write(self):
        self.observe()
        first = self.allowed()
        self.status_write()  # Between the ALLOW and the Executor's own read, as in r3.
        result = self.conflict()
        self.assertEqual(result["details"],
                         "Target identity or resource version changed; obtain a new proposal and validation.")
        self.assertEqual(result["conflict"], {"basis": "NOT_SENT", "resource_version": self.live_version(),
                                              "generation": self.incident["observed_generation"]})
        self.assertEqual(self.f.cluster.write_attempts, [])  # Nothing was sent for the first proposal.
        self.assert_repaired_by_replan(first, result)
        self.assertEqual(len(self.f.cluster.write_attempts), 1)

    def test_r2_a_status_write_inside_the_executors_patch_is_replanned_and_repaired_with_one_write(self):
        self.observe()
        first = self.allowed()
        self.write_races_the_patch()  # After the Executor's read and fresh Guard check, as in r2.
        result = self.conflict()
        self.assertEqual(result["details"], "Conditional write was refused without committing; refresh state "
                                            "and create a new proposal.")
        self.assertEqual(result["resource_version"], first["based_on_resource_version"])
        self.assertEqual(result["conflict"], {"basis": "REFUSED", "resource_version": self.live_version(),
                                              "generation": self.incident["observed_generation"]})
        self.assertEqual(len(self.f.cluster.write_attempts), 1)
        self.assertEqual(self.f.cluster.writes, [])
        self.assert_repaired_by_replan(first, result)
        self.assertEqual(len(self.f.cluster.write_attempts), 2)

    def test_a_spec_change_after_the_allow_is_final(self):
        """A ROLLBACK planned from the incident's evidence must not overwrite an operator's later fix."""
        for race in (False, True):
            with self.subTest(inside_the_patch=race):
                self.setUp()
                self.observe()
                first = self.allowed()
                bump = lambda deployment: deployment["metadata"].update(generation=deployment["metadata"]
                                                                        ["generation"] + 1)
                if race:
                    self.write_races_the_patch(bump)
                else:
                    self.f.cluster.external_write(bump)
                result = self.conflict()
                self.assertEqual(result["conflict"]["generation"], self.incident["observed_generation"] + 1)
                self.assert_conflict_final(first)

    def test_a_replaced_target_gives_no_conflict_record_and_is_final(self):
        self.observe()
        first = self.allowed()
        self.f.cluster.recreate("recreated-uid")
        result = self.conflict()
        self.assertNotIn("conflict", result)
        self.assert_conflict_final(first)

    def test_conflict_replans_share_the_stale_budget_and_the_last_conflict_stands(self):
        self.observe()
        proposal = self.plan()
        self.status_write()  # One stale re-plan first: attempt 1.
        self.assertEqual(self.guard_pass()["reason_codes"], ["STALE_RESOURCE_VERSION"])
        for attempt in range(2, self.stale_replans + 1):
            proposal = self.allowed()
            self.status_write()
            result = self.conflict()
            self.assertEqual(self.state(), "OPEN")
            replanned = self.plan()
            self.assertIn(f"Re-planned (attempt {attempt}) " + CONFLICT_NOTE, replanned["rationale"])
            self.assertEqual(replanned["based_on_resource_version"], result["conflict"]["resource_version"])
        self.allowed()
        self.status_write()
        self.conflict()
        self.assertEqual(self.state(), "FAILED")
        self.assertEqual(self.planner.once()["status"], "idle")
        incident = self.incident["incident_id"]
        self.assertEqual(self.f.store.reopenings(incident, STALE_REPLAN_CAUSE), 1)
        self.assertEqual(self.f.store.reopenings(incident, CONFLICT_REPLAN_CAUSE), self.stale_replans - 1)
        # Every re-plan had its own Planner journal row.
        keys = [replan_key(incident, attempt) for attempt in range(1, self.stale_replans + 1)]
        self.assertEqual(len({self.planning.get(key)["proposal"]["action_id"] for key in keys}), len(keys))
        self.assertEqual(self.f.cluster.write_attempts, [])
        self.assertEqual(self.active()["proposal"]["action"], proposal["action"])


class DefaultConflictTests(ConflictReplanFlow):
    """The Coordinator's default and Phase 3 file and kind modes: a CONFLICT is final, as before."""

    stale_replans = 0

    def test_without_replans_the_conflict_is_final_and_nothing_is_written(self):
        self.observe()
        first = self.allowed()
        self.status_write()
        self.assertEqual(self.conflict()["conflict"]["basis"], "NOT_SENT")
        self.assert_conflict_final(first)


def execution_result(proposal, status="CONFLICT", *, conflict=None, recorded_at="2026-10-10T12:00:50Z"):
    message = {"schema_version": "1.0", "kind": "execution_result", "incident_id": proposal["incident_id"],
               "action_id": proposal["action_id"], "target": proposal["target"], "status": status,
               "resource_version": proposal["based_on_resource_version"], "recorded_at": recorded_at,
               "details": "x"}
    if conflict is not None:
        message["conflict"] = conflict
    return message


class CoordinatorRuleTests(unittest.TestCase):
    """replan_after_conflict in the Coordinator's own transaction, without workers."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = OfflineFlow(directory.name)
        self.addCleanup(self.f.close)
        self.store = self.f.store
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)

    def record(self, message, limit=3):
        with self.store.transaction():
            return record_message(message, self.store, stale_replans=limit)

    def proposal(self, action="ROLLBACK"):
        journal = PlannerStore(":memory:", clock=lambda: self.f.now)
        self.addCleanup(journal.close)
        proposal = prepare(self.incident, journal=journal, baseline_reader=lambda _: [self.f.baseline],
                           retriever=retrieve_runbooks, provider=mock_provider)
        if action != "ROLLBACK":
            proposal = dict(proposal, action=action, action_id=proposal["action_id"] + "-" + action.lower())
            proposal["parameters"] = dict(proposal["parameters"], restart_token="RST-" + "0" * 16)
            validate_message(proposal)
        return proposal

    def evidence(self, **changes):
        values = {"basis": "NOT_SENT", "resource_version": "rv-live",
                  "generation": self.incident["observed_generation"]}
        values.update(changes)
        return values

    def conflicted(self, *, limit=3, action="ROLLBACK", before=None, **changes):
        """Record the incident and an ALLOWed proposal, then its CONFLICT; before(proposal) runs ahead of it."""
        self.record(self.incident, limit)
        proposal = self.proposal(action)
        self.record(proposal, limit)
        with self.store.transaction():
            self.store.save_guard_result(proposal["action_id"], guard_result(
                proposal, "ALLOW", ["POLICY_SATISFIED"], proposal["based_on_resource_version"]))
            self.store.set_state(self.incident["incident_id"], proposal["action_id"], "READY")
        if before is not None:
            before(proposal)
        conflict = changes.pop("conflict", self.evidence(**changes))
        self.record(execution_result(proposal, conflict=conflict), limit)
        return proposal

    def planner_requests(self):
        return [json.loads(row["payload"]) for row in self.store.db.execute(
            "SELECT payload FROM outbox WHERE topic='planner.request' ORDER BY id")]

    def state(self):
        return self.store.incident(self.incident["incident_id"])["state"]

    def assert_final(self):
        self.assertEqual(self.state(), "FAILED")
        self.assertEqual(len(self.planner_requests()), 1)

    def test_a_conflict_with_the_executors_record_queues_a_replan_on_the_version_it_read(self):
        proposal = self.conflicted()
        first, replan = self.planner_requests()
        self.assertEqual(replan, {"incident": first["incident"], "replan": {
            "attempt": 1, "after_action_id": proposal["action_id"], "resource_version": "rv-live",
            "cause": "EXECUTOR_CONFLICT"}})
        incident, key, attempt = planning_request(replan)
        self.assertEqual((incident["observed_resource_version"], key, attempt, replan_cause(replan)),
                         ("rv-live", replan_key(self.incident["incident_id"], 1), 1, "EXECUTOR_CONFLICT"))
        transition = self.store.transitions(self.incident["incident_id"])[-1]
        self.assertEqual((transition["from_state"], transition["to_state"], transition["cause"], transition["action_id"]),
                         ("FAILED", "OPEN", CONFLICT_REPLAN_CAUSE, proposal["action_id"]))
        self.assertEqual(self.store.action(proposal["action_id"])["state"], "FAILED")

    def test_a_conflict_without_the_record_is_final(self):
        """An older Executor, an unreadable target, a replaced UID, a write in doubt (FENCED included)."""
        self.conflicted(conflict=None)
        self.assert_final()

    def test_a_record_of_a_moved_spec_or_an_unmoved_version_is_final(self):
        for label, changes in (("spec changed", {"generation": self.incident["observed_generation"] + 1}),
                               ("version unmoved", {"resource_version": self.incident["observed_resource_version"]})):
            with self.subTest(label):
                self.setUp()
                self.conflicted(**changes)
                self.assert_final()

    def test_an_earlier_result_for_the_action_is_final(self):
        def in_doubt(proposal):
            self.record(execution_result(proposal, "NEEDS_RECONCILIATION", recorded_at="2026-10-10T12:00:45Z"))
        self.conflicted(before=in_doubt)
        self.assert_final()

    def test_a_revocation_a_second_approval_or_a_second_allow_is_final(self):
        record = {"action_id": "x", "operator_id": "op", "proposal_sha256": "0" * 64, "policy_version": "p",
                  "decision": "APPROVED", "approved_at": "2026-10-10T12:00:36Z"}

        def revoked(proposal):
            with self.store.transaction():
                self.store.add_approval(proposal["action_id"], dict(record, action_id=proposal["action_id"]))
                approval = self.store.approval_row(proposal["action_id"])["id"]
                revocation = {"approval_id": approval, "action_id": proposal["action_id"], "operator_id": "op",
                              "revoked_at": "2026-10-10T12:00:38Z", "reason": "withdrawn"}
                self.store.db.execute("INSERT INTO approval_revocations VALUES(?,?)",
                                      (approval, json.dumps(revocation)))

        def approved_twice(proposal):
            with self.store.transaction():
                for _ in range(2):
                    self.store.add_approval(proposal["action_id"], dict(record, action_id=proposal["action_id"]))

        def allowed_twice(proposal):
            with self.store.transaction():
                for state in ("VALIDATING", "READY"):
                    self.store.set_state(self.incident["incident_id"], proposal["action_id"], state)
        for before in (revoked, approved_twice, allowed_twice):
            with self.subTest(before.__name__):
                self.setUp()
                self.conflicted(before=before)
                self.assert_final()

    def test_a_sealed_action_is_final(self):
        def sealed(proposal):
            with self.store.transaction():
                self.store.require_seal(self.store.action(proposal["action_id"]))
                self.store.finish_seal(proposal["target"], 0, release=True)
        self.conflicted(before=sealed)
        self.assert_final()

    def test_a_restart_is_never_replanned(self):
        self.conflicted(action="RESTART")
        self.assert_final()

    def test_a_disabled_rule_or_a_spent_budget_queues_nothing(self):
        self.conflicted(limit=0)
        self.assert_final()
        self.setUp()

        def spent(proposal):
            with self.store.transaction():
                for _ in range(2):
                    self.store.reopen_incident(self.incident["incident_id"], proposal["action_id"],
                                               STALE_REPLAN_CAUSE)
                self.store.set_state(self.incident["incident_id"], proposal["action_id"], "READY")
        self.conflicted(limit=2, before=spent)
        self.assert_final()

    def test_a_replayed_conflict_queues_one_replan(self):
        proposal = self.conflicted()
        message = execution_result(proposal, conflict=self.evidence())
        self.assertFalse(self.record(message))
        self.assertEqual(len(self.planner_requests()), 2)
        with self.store.transaction():
            self.assertFalse(replan_after_conflict(message, self.store, limit=3))  # The incident is OPEN now.

    def test_other_messages_are_ignored(self):
        self.record(self.incident)
        proposal = self.proposal()
        self.record(proposal)
        with self.store.transaction():
            for status in ("FAILED", "SUPERSEDED", "APPLIED"):
                self.assertFalse(replan_after_conflict(execution_result(proposal, status), self.store, limit=3))
            self.assertFalse(replan_after_conflict(guard_result(proposal, "REJECT", ["STALE_RESOURCE_VERSION"], "rv"),
                                                   self.store, limit=3))
            # Not the active action's stored result (the action never reached the Executor).
            message = execution_result(proposal, conflict=self.evidence())
            self.assertFalse(replan_after_conflict(message, self.store, limit=3))
        self.assertEqual(self.state(), "VALIDATING")


class ExecutorRecordTests(unittest.TestCase):
    """What the Executor's CONFLICT result records, straight from execute() on the fake API."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = OfflineFlow(directory.name)
        self.addCleanup(self.f.close)
        self.f.prepare()
        self.f.authorize()
        self.assertEqual(self.f.state(), "READY")
        self.proposal = self.f.proposal
        self.generation = self.f.cluster.read(self.proposal["target"])["deployment"]["metadata"]["generation"]

    def test_not_sent_and_refused_records_name_the_live_version_and_generation(self):
        self.f.cluster.external_write()
        live = self.f.cluster.read(self.proposal["target"])["deployment"]["metadata"]["resourceVersion"]
        result = execute(self.proposal, self.f.adapters)
        self.assertEqual(result["conflict"], {"basis": "NOT_SENT", "resource_version": live,
                                              "generation": self.generation})
        validate_message(result)

    def test_a_refused_patch_records_the_read_made_after_it(self):
        original = self.f.cluster.conditional_restore

        def refused(**request):
            self.f.cluster.external_write()
            return original(**request)
        self.f.cluster.conditional_restore = refused
        result = execute(self.proposal, self.f.adapters)
        live = self.f.cluster.read(self.proposal["target"])["deployment"]["metadata"]["resourceVersion"]
        self.assertEqual(result["conflict"], {"basis": "REFUSED", "resource_version": live,
                                              "generation": self.generation})
        self.assertEqual(result["resource_version"], self.proposal["based_on_resource_version"])

    def test_no_record_without_a_usable_read(self):
        def unreadable(target):
            raise OSError("API unavailable")
        cases = {
            "unreadable after the refusal": lambda: setattr(self.f.cluster, "read", unreadable),
            "no generation": lambda: self.f.cluster.external_write(
                lambda deployment: deployment["metadata"].pop("generation")),
            "zero generation": lambda: self.f.cluster.external_write(
                lambda deployment: deployment["metadata"].update(generation=0)),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.setUp()
                original = self.f.cluster.conditional_restore

                def refused(change=change, original=original, **request):
                    self.f.cluster.external_write()
                    change()
                    return original(**request)
                self.f.cluster.conditional_restore = refused
                result = execute(self.proposal, self.f.adapters)
                self.assertEqual(result["status"], "CONFLICT")
                self.assertNotIn("conflict", result)

    def test_this_actions_evidence_on_the_target_is_adopted_not_conflicted(self):
        """A peer's write of the same action landed: APPLIED, never a CONFLICT that could be re-planned."""
        peer = replace(self.f.adapters, ledger=type(self.f.ledger)(self.f.path / "peer-executor.db"))
        self.addCleanup(peer.ledger.close)
        self.assertEqual(execute(self.proposal, peer)["status"], "APPLIED")
        self.assertEqual(execute(self.proposal, self.f.adapters)["status"], "APPLIED")


class ContractTests(unittest.TestCase):
    def setUp(self):
        target = {"cluster_id": "c", "namespace": "n", "kind": "Deployment", "name": "demo-api", "uid": "u"}
        self.message = {"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-1",
                        "action_id": "ACT-1", "target": target, "status": "CONFLICT",
                        "resource_version": "rv-0", "recorded_at": "2026-10-10T12:00:50Z", "details": "x",
                        "conflict": {"basis": "NOT_SENT", "resource_version": "rv-1", "generation": 3}}

    def test_a_conflict_record_is_optional_and_closed(self):
        validate_message(self.message)
        validate_message({key: value for key, value in self.message.items() if key != "conflict"})
        bad = [{"status": "FAILED"}, {"status": "APPLIED"}]
        for change in ({"basis": "FENCED"}, {"basis": None}, {"generation": 0}, {"generation": True},
                       {"generation": "3"}, {"resource_version": ""}, {"resource_version": "9" * 257},
                       {"extra": 1}):
            bad.append({"conflict": dict(self.message["conflict"], **change)})
        bad += [{"conflict": None}, {"conflict": "rv-1"}, {"conflict": {"basis": "NOT_SENT"}}]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_message(dict(self.message, **change))


class PlannerRequestTests(unittest.TestCase):
    def setUp(self):
        self.incident = detect(fixture("observations.json")["failed-rollout"], fixture("policy.json"))

    def request(self, **replan):
        values = {"attempt": 2, "after_action_id": "ACT-1", "resource_version": "rv-live",
                  "cause": "EXECUTOR_CONFLICT", **replan}
        return {"incident": self.incident, "replan": values}

    def test_a_conflict_replan_is_planned_like_a_stale_one_with_its_own_note(self):
        incident, key, attempt = planning_request(self.request())
        self.assertEqual((incident["observed_resource_version"], key, attempt),
                         ("rv-live", replan_key(self.incident["incident_id"], 2), 2))
        self.assertEqual(replan_cause(self.request()), "EXECUTOR_CONFLICT")
        self.assertIsNone(replan_cause({"incident": self.incident}))
        answer = ReplanProvider(DeterministicProvider(), 2, "EXECUTOR_CONFLICT")(
            {"incident": self.incident, "baselines": [fixture("baseline.json")], "runbooks": []})
        self.assertTrue(answer["rationale"].endswith(REPLAN_NOTES["EXECUTOR_CONFLICT"].format(attempt=2)))
        self.assertIn(CONFLICT_NOTE, answer["rationale"])

    def test_an_unknown_cause_is_refused(self):
        for cause in (None, "", "STALE_RESOURCE_VERSION", ["EXECUTOR_CONFLICT"], {"x": 1}, 7):
            with self.subTest(cause=cause), self.assertRaises(ValueError):
                planning_request(self.request(cause=cause))


    def test_a_stale_replan_still_names_no_cause_and_keeps_its_note(self):
        request = {"incident": self.incident, "replan": {"attempt": 1, "after_action_id": "ACT-1",
                                                         "resource_version": "rv-live"}}
        self.assertEqual(planning_request(request)[2], 1)
        self.assertIsNone(replan_cause(request))
        provider = ReplanProvider(DeterministicProvider(), 1, replan_cause(request))
        self.assertEqual(provider.note, REPLAN_NOTES[None].format(attempt=1))
        self.assertIn("after the Guard refused the previous proposal", provider.note)


if __name__ == "__main__":
    unittest.main()
