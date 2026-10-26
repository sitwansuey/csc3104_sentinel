"""Real Verifier -> Coordinator -> Guard integration, including revoked READY."""
from copy import deepcopy
import unittest

from services.coordinator import CoordinatorError, approve, handle
from services.verifier import inspect_dependencies
from tests import test_guard_integration as fixtures


class DependencyRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixtures.GuardIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)

    def finding(self, healthy=True, second=34):
        graph = fixtures.fixture('dependencies.json')
        graph['known_health']['mock-db'] = healthy
        return inspect_dependencies(self.flow.proposal, graph, self.flow.state,
                                    now=self.flow.time(second))

    def test_real_producer_clear_blocked_unknown(self):
        for health, verdict in ((True, 'ESCALATE'), (False, 'REJECT'), (None, 'ESCALATE')):
            with self.subTest(health=health):
                flow = fixtures.GuardIntegrationTests()
                flow.setUp()
                try:
                    graph = fixtures.fixture('dependencies.json')
                    graph['known_health']['mock-db'] = health
                    result = inspect_dependencies(flow.proposal, graph, flow.state, now=flow.time(34))
                    handle(result, flow.store, flow.bus)
                    decisions = flow.process(dependency_reader=None)
                    self.assertEqual(decisions[-1]['verdict'], verdict)
                    self.assertEqual(flow.execution_requests(), [])
                finally:
                    flow.doCleanups()

    def ready(self):
        flow = self.flow
        handle(self.finding(), flow.store, flow.bus)
        flow.process(dependency_reader=None)
        approve(flow.aid, 'operator', flow.store)
        flow.process(40, dependency_reader=None)
        self.assertEqual(flow.store.action(flow.aid)['state'], 'READY')

    def test_new_blocker_revokes_queued_execution_and_requires_fresh_guard(self):
        self.ready()
        flow = self.flow
        changed = self.finding(False, 41)
        self.assertTrue(handle(changed, flow.store, flow.bus))
        self.assertEqual(flow.store.action(flow.aid)['state'], 'VALIDATING')
        self.assertEqual(flow.execution_requests(), [])
        self.assertFalse(any(e['topic'] == 'executor.request' for e in flow.bus.events))
        self.assertFalse(handle(changed, flow.store, flow.bus))
        self.assertEqual(flow.process(42, dependency_reader=None)[-1]['verdict'], 'REJECT')
        self.assertEqual(flow.store.action(flow.aid)['state'], 'REJECTED')

    def test_clear_update_also_revalidates_instead_of_reusing_old_decision(self):
        self.ready()
        flow = self.flow
        handle(self.finding(True, 41), flow.store, flow.bus)
        self.assertEqual(flow.execution_requests(), [])
        self.assertEqual(flow.process(42, dependency_reader=None)[-1]['verdict'], 'ALLOW')
        self.assertEqual(len(flow.execution_requests()), 1)

    def test_older_or_mismatched_update_cannot_replace_current_evidence(self):
        self.ready()
        flow = self.flow
        for result in (self.finding(False, 33), self.finding(False, 39)):
            with self.assertRaises(CoordinatorError):
                handle(result, flow.store, flow.bus)
        wrong = self.finding(False, 41)
        wrong['target'] = dict(wrong['target'], uid='different')
        with self.assertRaises(CoordinatorError):
            handle(wrong, flow.store, flow.bus)
        self.assertEqual(flow.store.action(flow.aid)['state'], 'READY')
