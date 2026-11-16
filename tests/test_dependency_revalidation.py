"""Real Verifier -> Coordinator -> Guard integration, including revoked READY."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.coordinator.core import (
    CoordinatorError, LocalEventBus, SQLiteStore, approve, dispatch_pending, handle,
)
from services.guard.core import GuardInputError, evaluate, evaluate_with_report
from services.guard.common import json_sha256
from services.verifier.core import inspect_dependencies
from tests import test_guard_integration as fixtures


class DependencyRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.flow = self.new_flow()

    def new_flow(self):
        flow = fixtures.GuardIntegrationTests()
        flow.setUp()
        self.addCleanup(flow.doCleanups)
        return flow

    def finding(self, healthy=True, second=34, *, flow=None):
        flow = flow or self.flow
        graph = fixtures.fixture("dependencies.json")
        graph["known_health"]["mock-db"] = healthy
        return inspect_dependencies(flow.proposal, graph, flow.state, now=flow.time(second))

    def ready(self, *, flow=None, expires_at=None):
        flow = flow or self.flow
        handle(self.finding(flow=flow), flow.store, flow.bus)
        flow.process(dependency_reader=None)
        approve(flow.aid, "operator", flow.store, expires_at=expires_at)
        flow.process(40, dependency_reader=None)
        self.assertEqual(flow.store.action(flow.aid)["state"], "READY")

    def restart(self, flow):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = Path(directory) / "coordinator.sqlite3"
        with SQLiteStore(path) as persisted:
            flow.store.db.backup(persisted.db)
        flow.store = SQLiteStore(path, clock=lambda: flow.time(43))
        self.addCleanup(flow.store.close)
        flow.bus = LocalEventBus()
        dispatch_pending(flow.store, flow.bus)

    def test_real_producer_clear_blocked_unknown_and_policy(self):
        cases = ((True, "CLEAR", "ESCALATE", "ALLOW", "VALIDATION_PASSED"),
                 (False, "BLOCKED", "ESCALATE", "REJECT", "DEPENDENCY_BLOCKED"),
                 (None, "UNKNOWN", "ESCALATE", "ESCALATE", "DEPENDENCY_UNKNOWN"),
                 (None, "UNKNOWN", "REJECT", "REJECT", "DEPENDENCY_UNKNOWN"))
        for health, status, policy, verdict, reason in cases:
            with self.subTest(status=status, policy=policy):
                flow = self.new_flow()
                flow.policy["require_operator_approval"] = False
                flow.policy["unknown_dependency_result"] = policy
                result = self.finding(health, flow=flow)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["affected_services"], ["demo-api", "mock-db"])
                self.assertEqual(result["blockers"], ["mock-db"] if health is False else [])
                handle(result, flow.store, flow.bus)
                decisions = flow.process(dependency_reader=None)
                self.assertEqual(len(decisions), 1)
                self.assertEqual(decisions[0]["verdict"], verdict)
                self.assertIn(reason, decisions[0]["reason_codes"])
                self.assertEqual(len(flow.execution_requests()), int(verdict == "ALLOW"))

    def test_clear_still_requires_operator_approval(self):
        flow = self.flow
        handle(self.finding(), flow.store, flow.bus)
        decision = flow.process(dependency_reader=None)[0]
        self.assertEqual(decision["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        self.assertEqual(flow.execution_requests(), [])

    def test_new_blocker_revokes_queued_execution_and_requires_fresh_guard(self):
        self.ready()
        flow = self.flow
        changed = self.finding(False, 41)
        self.assertTrue(handle(changed, flow.store, flow.bus))
        self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
        self.assertEqual(flow.execution_requests(), [])
        self.assertFalse(any(e["topic"] == "executor.request" for e in flow.bus.events))
        self.assertFalse(handle(changed, flow.store, flow.bus))
        self.assertEqual(flow.process(42, dependency_reader=None)[0]["reason_codes"], ["DEPENDENCY_BLOCKED"])
        self.assertEqual(flow.store.action(flow.aid)["state"], "REJECTED")

    def test_unknown_update_revokes_ready_under_both_policy_modes(self):
        for policy in ("ESCALATE", "REJECT"):
            with self.subTest(policy=policy):
                flow = self.new_flow()
                flow.policy["unknown_dependency_result"] = policy
                self.ready(flow=flow)
                handle(self.finding(None, 41, flow=flow), flow.store, flow.bus)
                decision = flow.process(42, dependency_reader=None)[0]
                self.assertEqual(decision["verdict"], policy)
                self.assertEqual(decision["reason_codes"], ["DEPENDENCY_UNKNOWN"])
                self.assertEqual(flow.execution_requests(), [])
                with self.assertRaises(CoordinatorError):
                    approve(flow.aid, "operator", flow.store)

    def test_clear_update_also_revalidates_instead_of_reusing_old_decision(self):
        self.ready()
        flow = self.flow
        previous = flow.store.action(flow.aid)["guard_result"]
        changed = self.finding(True, 41)
        handle(changed, flow.store, flow.bus)
        self.assertEqual(flow.execution_requests(), [])
        decision = flow.process(42, dependency_reader=None)[0]
        self.assertEqual(decision["verdict"], "ALLOW")
        self.assertNotEqual(decision["inputs_sha256"], previous["inputs_sha256"])
        report = json.loads(flow.store.db.execute(
            "SELECT report FROM guard_reports WHERE decision_sha256=?", (json_sha256(decision),)).fetchone()[0])
        self.assertEqual(report["input_fingerprints"]["dependencies"], json_sha256(changed))
        self.assertEqual(len(flow.execution_requests()), 1)

    def test_older_equal_or_mismatched_update_cannot_replace_current_evidence(self):
        self.ready()
        flow = self.flow
        for second in (33, 34):
            with self.subTest(second=second), self.assertRaises(CoordinatorError):
                handle(self.finding(False, second), flow.store, flow.bus)
        for key, value in (("target", dict(flow.proposal["target"], uid="different")),
                           ("incident_id", "INC-OTHER"), ("action_id", "ACT-OTHER")):
            wrong = dict(self.finding(False, 41), **{key: value})
            with self.subTest(key=key), self.assertRaises(CoordinatorError):
                handle(wrong, flow.store, flow.bus)
        self.assertEqual(flow.store.action(flow.aid)["state"], "READY")
        self.assertEqual(flow.store.dependency_result(flow.aid), self.finding())

    def test_delayed_new_evidence_still_revokes_a_later_guard_decision(self):
        for second in (39, 40):
            with self.subTest(second=second):
                flow = self.new_flow()
                self.ready(flow=flow)
                handle(self.finding(False, second, flow=flow), flow.store, flow.bus)
                self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
                self.assertEqual(flow.execution_requests(), [])
                self.assertEqual(flow.process(42, dependency_reader=None)[0]["reason_codes"],
                                 ["DEPENDENCY_BLOCKED"])

    def test_first_received_dependency_finding_revokes_ready_conservatively(self):
        flow = self.flow
        guard = fixtures.fixture("messages/guard-result.json")
        guard.update(verdict="ALLOW", reason_codes=["VALIDATION_PASSED"],
                     checked_at="2026-10-10T12:00:40Z")
        handle(guard, flow.store, flow.bus)
        self.assertIsNone(flow.store.dependency_result(flow.aid))
        handle(self.finding(False, 39), flow.store, flow.bus)
        self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
        self.assertEqual(flow.execution_requests(), [])

    def test_future_finding_revokes_ready_and_follows_unknown_policy(self):
        self.ready()
        flow = self.flow
        handle(self.finding(True, 44), flow.store, flow.bus)
        self.assertEqual(flow.execution_requests(), [])
        decision = flow.process(42, dependency_reader=None)[0]
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_codes"], ["DEPENDENCY_RESULT_FROM_FUTURE"])
        self.assertEqual(flow.execution_requests(), [])

    def test_delayed_allow_predating_evidence_cannot_restore_ready(self):
        self.ready()
        flow = self.flow
        stale = evaluate(flow.proposal, flow.state, flow.policy, flow.baseline,
                         self.finding(), flow.store.approval(flow.aid), now=flow.time(41))
        handle(self.finding(False, 42), flow.store, flow.bus)
        with self.assertRaisesRegex(CoordinatorError, "predates"):
            handle(stale, flow.store, flow.bus)
        self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
        self.assertEqual(flow.execution_requests(), [])

    def test_later_allow_with_old_dependency_report_cannot_restore_ready(self):
        self.ready()
        flow = self.flow
        handle(self.finding(False, 41), flow.store, flow.bus)
        stale, report = evaluate_with_report(
            flow.proposal, flow.state, flow.policy, flow.baseline, self.finding(),
            flow.store.approval(flow.aid), now=flow.time(42))
        flow.store.db.execute("INSERT INTO guard_reports VALUES(?,?,?)",
                              (report["decision_sha256"], flow.aid, json.dumps(report)))
        with self.assertRaisesRegex(CoordinatorError, "bound to the latest evidence"):
            handle(stale, flow.store, flow.bus)
        legacy = {key: value for key, value in stale.items()
                  if key not in {"proposal_sha256", "inputs_sha256", "valid_until"}}
        legacy["schema_version"] = "1.0"
        with self.assertRaisesRegex(CoordinatorError, "bound to the latest evidence"):
            handle(legacy, flow.store, flow.bus)
        self.assertEqual(flow.execution_requests(), [])

    def test_changed_evidence_during_validation_or_approval_also_requires_binding(self):
        for state in ("VALIDATING", "AWAITING_APPROVAL"):
            with self.subTest(state=state):
                flow = self.new_flow()
                original = self.finding(flow=flow)
                handle(original, flow.store, flow.bus)
                if state == "AWAITING_APPROVAL":
                    flow.process(dependency_reader=None)
                else:
                    flow.policy["require_operator_approval"] = False
                self.assertEqual(flow.store.action(flow.aid)["state"], state)
                handle(self.finding(False, 36, flow=flow), flow.store, flow.bus)
                if state == "AWAITING_APPROVAL":
                    approve(flow.aid, "operator", flow.store)
                stale, report = evaluate_with_report(
                    flow.proposal, flow.state, flow.policy, flow.baseline, original,
                    flow.store.approval(flow.aid), now=flow.time(38))
                self.assertEqual(stale["verdict"], "ALLOW")
                flow.store.db.execute("INSERT INTO guard_reports VALUES(?,?,?)",
                                      (report["decision_sha256"], flow.aid, json.dumps(report)))
                with self.assertRaisesRegex(CoordinatorError, "bound to the latest evidence"):
                    handle(stale, flow.store, flow.bus)
                self.assertEqual(flow.execution_requests(), [])

    def test_worker_recomputes_input_binding_before_accepting_allow(self):
        self.ready()
        flow = self.flow
        changed = self.finding(True, 41)
        handle(changed, flow.store, flow.bus)
        stale, report = evaluate_with_report(
            flow.proposal, flow.state, flow.policy, flow.baseline, self.finding(),
            flow.store.approval(flow.aid), now=flow.time(42))
        # Even a report claiming the new dependency cannot detach the decision's
        # overall input hash from the actual inputs used by this worker.
        report["input_fingerprints"]["dependencies"] = json_sha256(changed)
        with patch("runtime.guard_pipeline.evaluate_with_report", return_value=(stale, report)):
            with self.assertRaises(GuardInputError):
                flow.process(42, dependency_reader=None)
        self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
        self.assertEqual(flow.execution_requests(), [])
        self.assertTrue(flow.pending_guards())

    def test_newest_stored_finding_wins_over_older_explicit_reader(self):
        self.ready()
        flow = self.flow
        handle(self.finding(False, 41), flow.store, flow.bus)
        decision = flow.process(42, dependency_reader=lambda p: self.finding(True, 34))[0]
        self.assertEqual(decision["reason_codes"], ["DEPENDENCY_BLOCKED"])
        self.assertEqual(flow.execution_requests(), [])

    def test_newer_explicit_reader_is_persisted_and_bound(self):
        self.ready()
        flow = self.flow
        handle(self.finding(None, 41), flow.store, flow.bus)
        latest = self.finding(True, 42)
        decision = flow.process(43, dependency_reader=lambda p: deepcopy(latest))[0]
        self.assertEqual(decision["verdict"], "ALLOW")
        self.assertEqual(flow.store.dependency_result(flow.aid), latest)
        self.assertEqual(flow.pending_guards(), [])
        self.assertEqual(flow.process(44), [])

    def test_equal_timestamp_provider_conflict_fails_closed_without_ack(self):
        self.ready()
        flow = self.flow
        changed = self.finding(False, 41)
        handle(changed, flow.store, flow.bus)
        with self.assertRaisesRegex(CoordinatorError, "same timestamp"):
            flow.process(42, dependency_reader=lambda p: self.finding(True, 41))
        self.assertEqual(flow.store.dependency_result(flow.aid), changed)
        self.assertEqual(flow.store.action(flow.aid)["state"], "VALIDATING")
        self.assertEqual(flow.execution_requests(), [])
        self.assertTrue(flow.pending_guards())

    def test_revalidation_and_report_requirement_survive_restart_and_approval(self):
        flow = self.flow
        self.ready(expires_at="2026-10-10T12:00:41Z")
        handle(self.finding(True, 41), flow.store, flow.bus)
        self.assertEqual(flow.process(42, dependency_reader=None)[0]["reason_codes"], ["APPROVAL_EXPIRED"])
        self.restart(flow)
        self.assertFalse(any(e["topic"] == "executor.request" for e in flow.bus.events))
        self.assertEqual(flow.store.action(flow.aid)["state"], "AWAITING_APPROVAL")
        approve(flow.aid, "operator", flow.store, expires_at="2026-10-10T12:00:55Z")
        unreported = evaluate(flow.proposal, flow.state, flow.policy, flow.baseline,
                              self.finding(True, 41), flow.store.approval(flow.aid), now=flow.time(44))
        with self.assertRaisesRegex(CoordinatorError, "bound to the latest evidence"):
            handle(unreported, flow.store, flow.bus)
        self.assertEqual(flow.process(44, dependency_reader=None)[0]["verdict"], "ALLOW")
        self.assertEqual(len(flow.execution_requests()), 1)

    def test_stale_dependency_evidence_cannot_be_approved_away(self):
        flow = self.flow
        flow.policy["max_dependency_age_seconds"] = 5
        handle(self.finding(), flow.store, flow.bus)
        flow.process(dependency_reader=None)
        approve(flow.aid, "operator", flow.store)
        decision = flow.process(40, dependency_reader=None)[0]
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_codes"], ["STALE_DEPENDENCY_RESULT"])
        self.assertEqual(flow.execution_requests(), [])
        with self.assertRaises(CoordinatorError):
            approve(flow.aid, "operator", flow.store)

    def test_dependency_update_after_execution_is_refused(self):
        self.ready()
        flow = self.flow
        handle(fixtures.fixture("messages/execution-result.json"), flow.store, flow.bus)
        with self.assertRaisesRegex(CoordinatorError, "out of order"):
            handle(self.finding(False, 46), flow.store, flow.bus)
        self.assertEqual(flow.store.action(flow.aid)["state"], "VERIFYING")


def cart_redis_lab(test, *, cart_ready=False):
    """redis-cart pod failure and a cart bad rollout (P4.05); cart calls redis-cart.

    cart_ready=False, the default, is what the live C1 runs show: the cart Pod
    with the bad REDIS_ADDR is NotReady. The Guard's dependency check counts
    callers, so it then also blocks the Redis repair. cart_ready=True is a
    hypothetical cart fault that leaves the cart Ready: only then is the Redis
    repair allowed, so the tests that use it check the re-plan mechanics, not
    a prediction of the live run.
    """
    from tests.support.pair_lab import PairLab
    directory = test.enterContext(tempfile.TemporaryDirectory())
    lab = PairLab(directory, {"redis-cart": "pod", "cartservice": "config"},
                  edges=[("frontend", "cartservice"), ("cartservice", "redis-cart")])
    test.addCleanup(lab.close)
    lab.health["cartservice"] = cart_ready
    return lab


class PairDependencyRevalidationTests(unittest.TestCase):
    """The dependency-ordered pair: a BLOCKED callee refuses the caller's repair until it recovers."""

    def replan(self, lab, name, attempt=1, cause="DEPENDENCY_RECOVERED"):
        from experiments.boutique.harness.multi_fault import replan
        from services.planner.core import mock_provider, retrieve_runbooks
        live = lab.cluster.read(lab.incidents[name]["target"])["deployment"]
        proposal = replan(lab.incidents[name], baselines=[lab.baselines[name]], retriever=retrieve_runbooks,
                          provider=mock_provider, live_template=live["spec"]["template"],
                          resource_version=live["metadata"]["resourceVersion"], attempt=attempt, cause=cause)
        handle(proposal, lab.store, lab.bus)
        return proposal

    def to_ready(self, lab, proposal):
        lab.assess(proposal)
        decision = lab.guard()
        self.assertEqual(lab.store.action(proposal["action_id"])["state"], "AWAITING_APPROVAL", decision)
        lab.approve(proposal, None)
        lab.assess(proposal)
        lab.guard()
        self.assertEqual(lab.store.action(proposal["action_id"])["state"], "READY")

    def test_blocked_redis_rejects_the_cart_repair_then_clear_allows_exactly_one_write(self):
        from experiments.boutique.harness.multi_fault import cross_incident_transitions, ordering_proof
        lab = cart_redis_lab(self, cart_ready=True)
        ids = lab.open()
        proposals = {proposal["target"]["name"]: proposal for proposal in lab.plan()}
        self.assertEqual({name: proposal["action"] for name, proposal in proposals.items()},
                         {"redis-cart": "RESTART", "cartservice": "ROLLBACK"})
        cart = proposals["cartservice"]
        lab.assess(cart)
        finding = lab.store.dependency_result(cart["action_id"])
        self.assertEqual((finding["status"], finding["blockers"]), ("BLOCKED", ["redis-cart"]))
        decision = next(item for item in lab.guard() if item["action_id"] == cart["action_id"])
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("REJECT", ["DEPENDENCY_BLOCKED"]))
        self.assertEqual(lab.state("cartservice"), "REJECTED")
        # The refusal is final for that action: newer CLEAR evidence cannot revive it.
        lab.health["redis-cart"] = True
        with self.assertRaises(CoordinatorError):
            handle(lab.dependency(cart), lab.store, lab.bus)
        lab.health["redis-cart"] = False

        self.to_ready(lab, proposals["redis-cart"])
        lab.execute()
        lab.verify()
        self.assertEqual(lab.state("redis-cart"), "RECOVERED")
        self.assertEqual((lab.writes("redis-cart"), lab.writes("cartservice")), (1, 0))

        again = self.replan(lab, "cartservice")
        self.assertNotEqual(again["action_id"], cart["action_id"])
        self.assertEqual(lab.state("cartservice"), "VALIDATING")
        self.to_ready(lab, again)
        self.assertEqual(lab.store.dependency_result(again["action_id"])["status"], "CLEAR")
        lab.execute()
        lab.verify()
        self.assertEqual((lab.state("redis-cart"), lab.state("cartservice")), ("RECOVERED", "RECOVERED"))
        self.assertEqual((lab.writes("redis-cart"), lab.writes("cartservice")), (1, 1))
        proof = ordering_proof(lab.store.transitions(), first_incident=ids["redis-cart"],
                               then_incident=ids["cartservice"])
        self.assertTrue(proof["passed"], proof)
        self.assertEqual(cross_incident_transitions(lab.store), [])

    def test_the_ordering_proof_fails_without_a_recovery_or_with_an_early_allow(self):
        from experiments.boutique.harness.multi_fault import ordering_proof
        rows = [{"incident_id": "INC-R", "to_state": "RECOVERED", "seq": 5, "recorded_at": "t5"},
                {"incident_id": "INC-C", "to_state": "READY", "seq": 7, "recorded_at": "t7"}]
        self.assertTrue(ordering_proof(rows, first_incident="INC-R", then_incident="INC-C")["passed"])
        early = rows + [{"incident_id": "INC-C", "to_state": "READY", "seq": 3, "recorded_at": "t3"}]
        proof = ordering_proof(early, first_incident="INC-R", then_incident="INC-C")
        self.assertFalse(proof["passed"])
        self.assertEqual(proof["then_ready_before_first_recovered"], [3])
        self.assertFalse(ordering_proof(rows[1:], first_incident="INC-R", then_incident="INC-C")["passed"])
        self.assertFalse(ordering_proof(rows[:1], first_incident="INC-R", then_incident="INC-C")["passed"])

    def run_pair(self, lab, advance=None):
        from experiments.boutique.harness.multi_fault import PairRunner
        ids = lab.open()
        order = {"redis-cart": ids["redis-cart"], "cartservice": ids["cartservice"]}
        runner = PairRunner(lab.store, order, advance=advance or lab.advance,
                            replan=lambda name, incident_id, attempt, cause: self.replan(lab, name, attempt, cause),
                            clock=lambda: lab.now.timestamp(), sleep=lab.tick)
        return ids, runner.run()

    def test_the_runner_waits_for_redis_then_replans_the_cart_once(self):
        lab = cart_redis_lab(self, cart_ready=True)
        ids, run = self.run_pair(lab)
        self.assertEqual(run["states"], {"redis-cart": "RECOVERED", "cartservice": "RECOVERED"})
        self.assertEqual(run["stops"], {})
        self.assertEqual((len(run["replans"]["redis-cart"]), len(run["replans"]["cartservice"])), (0, 1))
        self.assertEqual((lab.writes("redis-cart"), lab.writes("cartservice")), (1, 1))
        replanned = next(event for event in run["events"] if event["event"] == "replanned")
        self.assertEqual(replanned["after_recovered"], ["redis-cart"])

    def test_refusals_that_name_each_other_stop_both_without_a_write(self):
        from experiments.boutique.harness import scenarios
        from experiments.boutique.harness.multi_fault import pair_result, shop_outcome
        lab = cart_redis_lab(self)
        ids, run = self.run_pair(lab)
        self.assertEqual(run["stops"], {"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"})
        self.assertEqual(run["states"], {"redis-cart": "REJECTED", "cartservice": "REJECTED"})
        self.assertEqual((lab.writes("redis-cart"), lab.writes("cartservice"), lab.approvals), (0, 0, []))
        self.assertFalse(run["budget_exhausted"])
        pair = scenarios.pair("redis-pod+cart-config")
        graded = pair_result(pair, shop=shop_outcome(run["states"], []), stops=run["stops"], writes={})
        self.assertEqual(graded["result"], "DEPENDENCY_DEADLOCK")
        self.assertTrue(graded["acceptable"])
        self.assertFalse(graded["matched_expected"])

    def test_a_not_ready_cart_blocks_the_redis_repair_under_the_current_rule(self):
        # The Guard counts callers as well as callees (inspect_dependencies), so the C1 cart, NotReady as in
        # the live runs, refuses the Redis RESTART. Changing that rule is an owner decision
        # (L-CALLER-BLOCKS-CALLEE-REPAIR); this test changes with it, on purpose.
        lab = cart_redis_lab(self)
        lab.open()
        redis = next(proposal for proposal in lab.plan() if proposal["target"]["name"] == "redis-cart")
        self.assertEqual(redis["action"], "RESTART")
        lab.assess(redis)
        finding = lab.store.dependency_result(redis["action_id"])
        self.assertEqual((finding["status"], finding["blockers"]), ("BLOCKED", ["cartservice"]))
        decision = next(item for item in lab.guard() if item["action_id"] == redis["action_id"])
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("REJECT", ["DEPENDENCY_BLOCKED"]))
        self.assertEqual((lab.writes("redis-cart"), lab.writes("cartservice")), (0, 0))

    def test_a_partner_blocked_by_a_third_service_is_not_a_deadlock(self):
        from unittest.mock import Mock
        from experiments.boutique.harness.multi_fault import PairRunner
        store = Mock()
        incidents = {"redis-cart": {"state": "REJECTED", "active_action_id": "ACT-REDIS"},
                     "cartservice": {"state": "REJECTED", "active_action_id": "ACT-CART"}}
        blockers = {"ACT-REDIS": ["cartservice"], "ACT-CART": ["frontend"]}
        store.incident.side_effect = lambda incident_id: incidents[incident_id]
        store.action.side_effect = lambda action_id: {"guard_result": {"reason_codes": ["DEPENDENCY_BLOCKED"]}}
        store.dependency_result.side_effect = lambda action_id: {"blockers": blockers[action_id]}
        ticks = iter(range(1000))
        runner = PairRunner(store, {"redis-cart": "redis-cart", "cartservice": "cartservice"}, advance=Mock(),
                            replan=Mock(), clock=lambda: next(ticks), sleep=Mock(), budget_seconds=5)
        runner.after_rejection("redis-cart")
        self.assertEqual(runner.stops, {})  # The cart refusal does not name Redis: no mutual block.
        run = runner.run()
        self.assertEqual(run["stops"], {"cartservice": "REJECTED", "redis-cart": "BLOCKER_NOT_RECOVERED"})
        blockers["ACT-CART"] = ["redis-cart"]
        runner = PairRunner(store, {"redis-cart": "redis-cart", "cartservice": "cartservice"}, advance=Mock(),
                            replan=Mock(), clock=lambda: next(ticks), sleep=Mock(), budget_seconds=5)
        self.assertEqual(runner.run()["stops"], {"redis-cart": "DEPENDENCY_DEADLOCK",
                                                 "cartservice": "DEPENDENCY_DEADLOCK"})

    def test_a_dependency_that_fails_again_ends_after_one_replan(self):
        lab = cart_redis_lab(self, cart_ready=True)

        def advance(name, incident_id):
            state = lab.advance(name, incident_id)
            if name == "redis-cart" and state == "RECOVERED":
                lab.health["redis-cart"] = False  # Redis drops again after its verified repair.
            return state

        ids, run = self.run_pair(lab, advance)
        self.assertEqual(run["stops"], {"cartservice": "REPLAN_BUDGET_EXHAUSTED"})
        self.assertEqual(len(run["replans"]["cartservice"]), 1)
        self.assertEqual(lab.writes("cartservice"), 0)
        self.assertEqual(lab.state("redis-cart"), "RECOVERED")


if __name__ == "__main__":
    unittest.main()
