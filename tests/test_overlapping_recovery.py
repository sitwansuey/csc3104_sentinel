"""A new rollout fault during verification must survive and recover separately."""
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.planner_pipeline import process_planner_requests
from services.coordinator.core import approve, handle
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from tests.test_verifier_pipeline import grpc_policy, grpc_samples


class OverlappingRecoveryTests(unittest.TestCase):
    def test_second_fault_invalidates_first_outcome_then_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            flow = OfflineFlow(Path(directory))
            try:
                flow.policy["health"] = grpc_policy()["health"]
                flow.prepare()
                first_id = flow.incident["incident_id"]
                flow.authorize()
                flow.execute()
                first = flow.store.action(flow.proposal["action_id"])["execution_result"]
                self.assertEqual(flow.state(), "VERIFYING")

                # A second external configuration fault arrives while the first
                # action is still being verified, before its healthy window ends.
                flow.now += timedelta(seconds=5)
                flow.cluster.snapshot["deployment"]["spec"]["template"] = deepcopy(
                    fixture("observations.json")["failed-rollout"][-1]["deployment"]["spec"]["template"])
                meta = flow.cluster.snapshot["deployment"]["metadata"]
                meta.update(generation=3, resourceVersion="second-fault-version")
                flow.cluster.snapshot["deployment"]["status"]["observedGeneration"] = 3
                observations = [flow.cluster.read(first["target"])]
                flow.now += timedelta(seconds=30)
                observations.append(flow.cluster.read(first["target"]))
                second = detect(observations, flow.policy)
                self.assertIsNotNone(second)
                handle(second, flow.store, flow.bus)
                provider = Mock(side_effect=mock_provider)
                def plan():
                    return process_planner_requests(flow.store, flow.bus,
                        baseline_reader=lambda _: [flow.baseline], retriever=retrieve_runbooks, provider=provider)
                self.assertEqual(plan(), [])
                provider.assert_not_called()

                readings = grpc_samples(first, flow.proposal["parameters"]["template_sha256"])
                for reading in readings[1:]:
                    reading.update(grpc_status=14, ready_replicas=0, template_sha256="b" * 64)
                self.assertEqual(flow.verify(readings)[0]["status"], "NOT_RECOVERED")
                self.assertEqual(flow.store.incident(first_id)["state"], "FAILED")

                flow.restart()
                flow.incident = second
                flow.proposal = plan()[0]
                flow.guard()
                self.assertEqual(flow.state(), "AWAITING_APPROVAL")
                flow.now += timedelta(seconds=1)
                approve(flow.proposal["action_id"], "offline-operator", flow.store)
                flow.now += timedelta(seconds=1)
                flow.guard()
                self.assertEqual(flow.state(), "READY")
                flow.execute()
                self.assertEqual(flow.state(), "VERIFYING")
                execution = flow.store.action(flow.proposal["action_id"])["execution_result"]
                flow.now += timedelta(seconds=12)
                self.assertEqual(flow.verify(grpc_samples(execution,
                    flow.proposal["parameters"]["template_sha256"]))[0]["status"], "RECOVERED")
                self.assertEqual(flow.state(), "RECOVERED")
                self.assertEqual(flow.store.incident(first_id)["state"], "FAILED")
                self.assertEqual(flow.execute(), [])
                self.assertEqual(len(flow.cluster.writes), 2)
                self.assertEqual(provider.call_count, 1)
            finally:
                flow.close()


class TwoTargetRecoveryTests(unittest.TestCase):
    """Two simultaneous faults on two targets (P4.05) through the real Coordinator, Guard, Executor and Verifier."""

    def lab(self, faults=None):
        from tests.support.pair_lab import PairLab
        directory = self.enterContext(tempfile.TemporaryDirectory())
        lab = PairLab(directory, faults or {"shippingservice": "config", "cartservice": "pod"},
                      edges=[("frontend", "shippingservice"), ("frontend", "cartservice")])
        self.addCleanup(lab.close)
        return lab

    def runner(self, lab, ids, **options):
        from experiments.boutique.harness.multi_fault import PairRunner
        options.setdefault("advance", lab.advance)
        options.setdefault("replan", Mock(side_effect=AssertionError("no re-plan expected")))
        return PairRunner(lab.store, ids, clock=lambda: lab.now.timestamp(), sleep=lab.tick, **options)

    def test_both_incidents_reach_ready_together_and_each_gets_one_write(self):
        lab = self.lab()
        ids = lab.open()
        proposals = {proposal["target"]["name"]: proposal for proposal in lab.plan()}
        self.assertEqual({name: proposal["action"] for name, proposal in proposals.items()},
                         {"shippingservice": "ROLLBACK", "cartservice": "RESTART"})
        for proposal in proposals.values():
            lab.assess(proposal)
        lab.guard()
        for proposal in proposals.values():
            lab.approve(proposal, None)
        lab.guard()
        self.assertEqual((lab.state("shippingservice"), lab.state("cartservice")), ("READY", "READY"))
        results = lab.execute()
        self.assertEqual(sorted(result["status"] for result in results), ["APPLIED", "APPLIED"])
        self.assertEqual((lab.writes("shippingservice"), lab.writes("cartservice")), (1, 1))
        lab.verify()
        self.assertEqual((lab.state("shippingservice"), lab.state("cartservice")), ("RECOVERED", "RECOVERED"))
        self.assertEqual(lab.execute(), [])
        self.assertEqual((lab.writes("shippingservice"), lab.writes("cartservice")), (1, 1))
        self.assertEqual(set(ids), {"shippingservice", "cartservice"})

    def test_a_second_incident_on_the_same_target_waits_behind_the_first(self):
        from services.coordinator.core import CoordinatorError
        lab = self.lab()
        lab.open()
        first = next(proposal for proposal in lab.plan() if proposal["target"]["name"] == "shippingservice")
        second = dict(deepcopy(lab.incidents["shippingservice"]), incident_id="INC-SHIPPING-SECOND")
        handle(second, lab.store, lab.bus)
        self.assertEqual(lab.plan(), [])  # planning_blocker: the workload is already being recovered.
        with self.assertRaisesRegex(CoordinatorError, "Another incident is recovering this workload"):
            handle(dict(first, incident_id=second["incident_id"], action_id="ACT-SHIPPING-SECOND"),
                   lab.store, lab.bus)
        self.assertEqual(lab.store.incident("INC-SHIPPING-SECOND")["state"], "OPEN")
        self.assertEqual(lab.state("shippingservice"), "VALIDATING")

    def test_the_runner_recovers_an_independent_pair_with_one_write_each(self):
        from experiments.boutique.harness import scenarios
        from experiments.boutique.harness.multi_fault import (cross_incident_transitions, incident_timings,
                                                              pair_result, shop_outcome, writes_by_target)
        lab = self.lab()
        ids = lab.open()
        run = self.runner(lab, ids).run()
        self.assertEqual(run["states"], {"shippingservice": "RECOVERED", "cartservice": "RECOVERED"})
        self.assertEqual((run["stops"], run["budget_exhausted"]), ({}, False))
        self.assertEqual(writes_by_target(lab.ledger.db), {"shippingservice": 1, "cartservice": 1})
        self.assertEqual(cross_incident_transitions(lab.store), [])
        timings = incident_timings(lab.store.transitions(), ids)
        self.assertTrue(all(item["seconds_to_recovered"] > 0 for item in timings.values()))
        shop = shop_outcome(run["states"], [{"passed": True}] * 3)
        graded = pair_result(scenarios.pair("shipping-config+cart-pod"), shop=shop, stops=run["stops"],
                             writes=writes_by_target(lab.ledger.db))
        self.assertEqual((graded["result"], graded["matched_expected"], graded["labels_pending"]),
                         ("RECOVERED", True, True))

    def test_one_failed_repair_does_not_fail_the_other_target(self):
        from experiments.boutique.harness.multi_fault import shop_outcome
        lab = self.lab()
        ids = lab.open()
        run = self.runner(lab, ids, advance=lambda name, incident_id: lab.advance(
            name, incident_id, verify_health={"cartservice": False})).run()
        self.assertEqual(run["states"], {"shippingservice": "RECOVERED", "cartservice": "FAILED"})
        shop = shop_outcome(run["states"], [{"passed": True}] * 3)
        self.assertEqual((shop["status"], shop["recovered"], shop["not_recovered"]),
                         ("PARTIAL", ["shippingservice"], ["cartservice"]))

    def test_a_refused_approval_stops_only_that_incident(self):
        from experiments.boutique.harness.multi_fault import ApprovalRefused

        def approve(proposal, decision):
            if proposal["target"]["name"] == "cartservice":
                raise ApprovalRefused(proposal["action_id"])
            return lab.approve(proposal, decision)

        lab = self.lab()
        ids = lab.open()
        run = self.runner(lab, ids, advance=lambda name, incident_id: lab.advance(
            name, incident_id, approve=approve)).run()
        self.assertEqual(run["stops"], {"cartservice": "APPROVAL_REFUSED"})
        self.assertEqual(run["states"], {"shippingservice": "RECOVERED", "cartservice": "AWAITING_APPROVAL"})
        self.assertEqual((lab.writes("shippingservice"), lab.writes("cartservice")), (1, 0))

    def test_an_escalation_or_a_non_dependency_rejection_stops_without_a_retry(self):
        lab = self.lab()
        lab.health.pop("frontend")  # Unknown caller readiness: the Guard escalates DEPENDENCY_UNKNOWN.
        ids = lab.open()
        replan = Mock()
        run = self.runner(lab, ids, replan=replan).run()
        self.assertEqual(run["stops"], {"shippingservice": "ESCALATED", "cartservice": "ESCALATED"})
        self.assertIn("DEPENDENCY_UNKNOWN", run["events"][-1]["reason_codes"])
        lab = self.lab()
        lab.policies["shippingservice"]["allowed_actions"] = ["RESTART"]
        ids = lab.open()
        run = self.runner(lab, ids, replan=replan).run()
        self.assertEqual(run["stops"], {"shippingservice": "REJECTED"})
        self.assertEqual(run["states"]["cartservice"], "RECOVERED")
        replan.assert_not_called()
        self.assertEqual(lab.writes("shippingservice"), 0)

    def test_a_refused_replan_or_a_blocker_that_stops_ends_the_wait(self):
        from experiments.boutique.harness.multi_fault import PairRunner
        from services.coordinator.core import CoordinatorError
        store = Mock()
        incidents = {"a": {"state": "REJECTED", "active_action_id": "ACT-A"},
                     "b": {"state": "RECOVERED", "active_action_id": "ACT-B"}}
        store.incident.side_effect = lambda incident_id: incidents[incident_id]
        store.action.side_effect = lambda action_id: {"guard_result": {"reason_codes": ["DEPENDENCY_BLOCKED"]}}
        store.dependency_result.side_effect = lambda action_id: {"blockers": ["b"] if action_id == "ACT-A" else []}
        def bounded(**options):
            # A bounded clock, so a regression ends at the budget instead of waiting forever.
            ticks = iter(range(1000))
            return PairRunner(store, {"a": "a", "b": "b"}, advance=Mock(), clock=lambda: next(ticks), sleep=Mock(),
                              budget_seconds=5, **options)

        runner = bounded(replan=Mock(side_effect=CoordinatorError("Restart budget for this incident is exhausted")))
        self.assertEqual(runner.run()["stops"], {"a": "REPLAN_REFUSED"})
        incidents["b"]["state"] = "AWAITING_APPROVAL"
        runner = bounded(replan=Mock())
        runner.stops["b"] = "APPROVAL_REFUSED"
        self.assertEqual(runner.run()["stops"]["a"], "BLOCKER_NOT_RECOVERED")
        incidents["b"]["state"] = "FAILED"
        runner = bounded(replan=Mock())
        self.assertEqual(runner.run()["stops"], {"a": "BLOCKER_NOT_RECOVERED"})
        # A blocker outside the pair is a plain refusal: nothing in the pair can clear it.
        store.dependency_result.side_effect = lambda action_id: {"blockers": ["frontend"]}
        runner = bounded(replan=Mock())
        self.assertEqual(runner.run()["stops"], {"a": "REJECTED"})

    def test_write_counts_other_than_one_per_target_are_never_recovered(self):
        from experiments.boutique.harness import scenarios
        from experiments.boutique.harness.multi_fault import pair_result, shop_outcome
        pair = scenarios.pair("shipping-config+cart-pod")
        both = {"shippingservice": "RECOVERED", "cartservice": "RECOVERED"}
        shop = shop_outcome(both, [{"passed": True}] * 3)
        for writes in ({"shippingservice": 2, "cartservice": 1}, {"shippingservice": 0, "cartservice": 1},
                       {"cartservice": 1}, {"shippingservice": 1, "cartservice": 3}):
            with self.subTest(writes=writes):
                graded = pair_result(pair, shop=shop, stops={}, writes=writes)
                self.assertEqual(graded["result"], "UNEXPECTED_WRITES")
                self.assertFalse(graded["matched_expected"] or graded["acceptable"])
        partial = shop_outcome({"shippingservice": "RECOVERED", "cartservice": "FAILED"}, [{"passed": True}] * 3)
        self.assertEqual(pair_result(pair, shop=partial, stops={}, writes={"shippingservice": 1, "cartservice": 1})
                         ["result"], "PARTIAL")

    def test_a_deadlock_needs_a_dependency_pair_both_stopped_and_no_write(self):
        from experiments.boutique.harness import scenarios
        from experiments.boutique.harness.multi_fault import pair_result, shop_outcome
        dependent, independent = scenarios.pair("redis-pod+cart-config"), scenarios.pair("shipping-config+cart-pod")
        rejected = {"redis-cart": "REJECTED", "cartservice": "REJECTED"}
        both = {"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"}
        shop = shop_outcome(rejected, [], stopped=both)
        self.assertEqual(pair_result(dependent, shop=shop, stops=both, writes={})["result"], "DEPENDENCY_DEADLOCK")
        for stops, writes in ((both, {"redis-cart": 1}), ({"cartservice": "DEPENDENCY_DEADLOCK"}, {}),
                              ({"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "REJECTED"}, {})):
            with self.subTest(stops=stops, writes=writes):
                self.assertEqual(pair_result(dependent, shop=shop, stops=stops, writes=writes)["result"], "FAILED")
        renamed = {"shippingservice": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"}
        self.assertEqual(pair_result(independent, shop=shop, stops=renamed, writes={})["result"], "FAILED")

    def test_only_a_recovered_result_with_a_passing_proof_shows_an_ordered_recovery(self):
        from experiments.boutique.harness import scenarios
        from experiments.boutique.harness.multi_fault import pair_result, shop_outcome
        dependent, independent = scenarios.pair("redis-pod+cart-config"), scenarios.pair("shipping-config+cart-pod")
        recovered = shop_outcome({"redis-cart": "RECOVERED", "cartservice": "RECOVERED"}, [{"passed": True}] * 3)
        writes = {"redis-cart": 1, "cartservice": 1}
        for ordering, shown in (({"passed": True}, True), ({"passed": False}, False), (None, False),
                                ({"passed": "yes"}, False)):
            with self.subTest(ordering=ordering):
                graded = pair_result(dependent, shop=recovered, stops={}, writes=writes, ordering=ordering)
                self.assertIs(graded["ordered_recovery_shown"], shown)
        both = {"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"}
        deadlock = pair_result(dependent, shop=shop_outcome({"redis-cart": "REJECTED", "cartservice": "REJECTED"}, []),
                               stops=both, writes={}, ordering={"passed": True})
        self.assertEqual((deadlock["result"], deadlock["acceptable"], deadlock["ordered_recovery_shown"]),
                         ("DEPENDENCY_DEADLOCK", True, False))
        self.assertIsNone(pair_result(independent, shop=recovered, stops={}, writes={})["ordered_recovery_shown"])

    def test_the_budget_bounds_a_pair_that_never_settles(self):
        from experiments.boutique.harness.multi_fault import PairRunner, shop_outcome
        lab = self.lab()
        ids = lab.open()
        ticks = iter(range(10_000))
        runner = PairRunner(lab.store, ids, advance=lambda name, incident_id: lab.state(name), replan=Mock(),
                            budget_seconds=10, poll_seconds=2, clock=lambda: next(ticks), sleep=Mock())
        run = runner.run()
        self.assertTrue(run["budget_exhausted"])
        self.assertEqual(run["states"], {"shippingservice": "OPEN", "cartservice": "OPEN"})
        self.assertEqual(shop_outcome(run["states"], [{"passed": True}] * 3)["status"], "UNKNOWN")
        with self.assertRaises(ValueError):
            PairRunner(lab.store, {"only": "INC-1"}, advance=Mock(), replan=Mock())
