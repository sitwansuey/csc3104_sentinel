from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from experiments.validation.demo_offline_flow import OfflineFlow, fixture, demonstrate
from services.coordinator.core import handle
from runtime.planner_pipeline import process_planner_requests
from services.planner.core import retrieve_runbooks, mock_provider


class OfflineFlowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.flow = OfflineFlow(directory.name)
        self.addCleanup(self.flow.close)
        self.flow.prepare()

    def test_demo_runs_all_real_components_and_preserves_partial_verification(self):
        demonstrate()

    def test_missing_approval_and_policy_rejection_do_not_execute(self):
        flow = self.flow
        flow.guard()
        self.assertEqual(flow.state(), 'AWAITING_APPROVAL')
        self.assertEqual(flow.execute(), [])
        self.assertEqual(flow.cluster.writes, [])
        flow.policy['targets'] = []
        flow.authorize()
        self.assertEqual(flow.state(), 'REJECTED')
        self.assertEqual(flow.cluster.writes, [])

    def test_stale_state_rejected_at_real_executor_gate(self):
        flow = self.flow
        flow.authorize()
        flow.cluster.snapshot['deployment']['metadata']['resourceVersion'] = 'changed'
        flow.execute()
        self.assertEqual(flow.state(), 'FAILED')
        self.assertEqual(flow.cluster.writes, [])

    def test_unhealthy_outcome_is_failed_and_applied_is_only_verifying(self):
        flow = self.flow
        flow.authorize()
        flow.execute()
        self.assertEqual(flow.state(), 'VERIFYING')
        flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=timezone.utc)
        flow.verify(fixture('health.json')['unstable'])
        self.assertEqual(flow.state(), 'FAILED')

    def test_pending_execution_restart_and_delivery_failure_do_not_repeat_effects(self):
        flow = self.flow
        flow.authorize()
        flow.restart()
        with patch('runtime.executor_pipeline.record_message', side_effect=OSError('delivery interrupted')):
            with self.assertRaises(OSError):
                flow.execute()
        self.assertEqual(len(flow.cluster.writes), 1)
        flow.restart()
        flow.execute()
        self.assertEqual(flow.state(), 'VERIFYING')
        self.assertEqual(len(flow.cluster.writes), 1)
        self.assertEqual(flow.execute(), [])

    def test_duplicate_incident_and_planner_handoff_create_no_new_action(self):
        flow = self.flow
        self.assertFalse(handle(flow.incident, flow.store, flow.bus))
        proposals = process_planner_requests(flow.store, flow.bus, baseline_reader=lambda i: [flow.baseline],
            retriever=retrieve_runbooks, provider=mock_provider)
        self.assertEqual(proposals, [])
        self.assertEqual(flow.store.db.execute('SELECT count(*) FROM actions').fetchone()[0], 1)
