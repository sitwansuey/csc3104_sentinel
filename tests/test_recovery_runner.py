import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance
from scripts.demo_offline_flow import OfflineFlow, fixture
from services.coordinator import handle
from services.observer import detect
from services.planner import mock_provider, retrieve_runbooks
from services.verifier import inspect_dependencies


class RunnerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.f = OfflineFlow(directory.name)
        self.addCleanup(self.f.close)
        self.incident = detect(fixture('observations.json')['failed-rollout'], self.f.policy)
        handle(self.incident, self.f.store, self.f.bus)
        self.operator = lambda p: 'operator'
        self.provider = mock_provider
        self.readings = []
        self.block_after_approval = False

    def step(self):
        f = self.f
        def plan():
            process_planner_requests(f.store, f.bus, baseline_reader=lambda i: [f.baseline],
                                     retriever=retrieve_runbooks, provider=self.provider)
        def assess(p):
            f.now += timedelta(seconds=1)
            finding = inspect_dependencies(p, f.graph, f.cluster.read(p['target']), now=f.now)
            if self.block_after_approval and f.store.approval(p['action_id']):
                finding.update(status="BLOCKED", blockers=["new dependency failure"])
            handle(finding, f.store, f.bus)
        def guard():
            from runtime.guard_pipeline import process_guard_requests
            process_guard_requests(f.store, f.bus, state_reader=lambda p: f.cluster.read(p['target']),
                policy_reader=lambda p: f.policy, baseline_reader=lambda p: f.baseline, clock=lambda: f.now)
        return advance(f.store, self.incident['incident_id'], plan=plan, assess=assess,
                       guard=guard, operator=self.operator, execute=lambda p: f.execute(),
                       verify=lambda p, r: f.verify(self.readings))

    def test_replay_and_restart_do_not_repeat_the_repair(self):
        self.assertEqual(self.step(), 'VERIFYING')
        self.f.restart()
        handle(self.incident, self.f.store, self.f.bus)
        self.f.now = self.f.now.replace(minute=1, second=10)
        self.readings = fixture('health.json')['stable']
        self.assertEqual(self.step(), 'RECOVERED')
        self.assertEqual(self.step(), 'RECOVERED')
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_refused_approval_makes_no_write(self):
        self.operator = lambda p: None
        self.assertEqual(self.step(), 'AWAITING_APPROVAL')
        self.assertEqual(self.f.cluster.writes, [])

    def test_changed_dependency_makes_no_write(self):
        self.block_after_approval = True
        self.assertEqual(self.step(), 'REJECTED')
        self.assertEqual(self.f.cluster.writes, [])

    def test_invalid_or_unavailable_provider_preserves_pending_work(self):
        for answer in (lambda ctx: {}, lambda ctx: (_ for _ in ()).throw(TimeoutError())):
            self.provider = answer
            with self.assertRaises((ValueError, TimeoutError)):
                self.step()
            self.f.restart()
            self.assertEqual(self.f.store.incident(self.incident['incident_id'])['state'], 'OPEN')
            self.assertEqual(self.f.cluster.writes, [])
        self.provider = mock_provider
        self.assertEqual(self.step(), 'VERIFYING')

    def test_crash_at_executor_handoff_resumes_without_extra_write(self):
        with patch('runtime.executor_pipeline.record_message', side_effect=OSError('lost handoff')):
            with self.assertRaises(OSError):
                self.step()
        self.f.restart()
        self.assertEqual(self.step(), 'VERIFYING')
        self.assertEqual(len(self.f.cluster.writes), 1)

    def test_unhealthy_result_never_marks_recovery(self):
        self.assertEqual(self.step(), 'VERIFYING')
        self.f.now = self.f.now.replace(minute=1, second=10)
        self.readings = fixture('health.json')['unstable']
        self.assertEqual(self.step(), 'FAILED')

    def test_missing_evidence_times_out_after_restart(self):
        self.assertEqual(self.step(), 'VERIFYING')
        self.f.restart()
        self.f.now += timedelta(seconds=90)
        self.assertEqual(self.step(), 'NEEDS_REVIEW')
        self.assertEqual(len(self.f.cluster.writes), 1)
