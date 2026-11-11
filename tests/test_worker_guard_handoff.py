"""Offline cross-branch regression: remote Guard results need their bound report."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

from runtime.worker_http import handler, WorkerClient
from runtime.worker_transport import Delivery, ProtocolError, digest
from services.coordinator.core import SQLiteStore, approve, handle
from services.guard.core import evaluate_with_report
from tests import test_guard_integration as fixtures


class WorkerGuardHandoffTests(unittest.TestCase):
    def setUp(self):
        self.flow = fixtures.GuardIntegrationTests()
        self.flow.setUp()
        self.addCleanup(self.flow.doCleanups)
        flow = self.flow
        self.delivery = Delivery(flow.store, clock=lambda: 1)
        flow.policy['require_operator_approval'] = False
        first = deepcopy(flow.dependencies)
        handle(first, flow.store, flow.bus)
        self.latest = dict(first, checked_at=flow.time(41).isoformat().replace('+00:00', 'Z'))
        handle(self.latest, flow.store, flow.bus)
        self.claim = self.delivery.claim('guard-1', 'guard')['delivery']
        self.decision, self.report = evaluate_with_report(flow.proposal, flow.state,
            flow.policy, flow.baseline, self.latest, now=flow.time(42))
        self.assertEqual(self.decision['verdict'], 'ALLOW')

    def finish(self, *, report=None):
        return self.delivery.finish('guard-1', 'guard', self.claim['delivery_id'],
                                    self.claim['lease_token'], self.decision, report=report)

    def test_missing_report_cannot_acknowledge_or_authorize_revalidated_action(self):
        with self.assertRaises(ProtocolError):
            self.finish()
        self.assertEqual(self.flow.store.action(self.flow.aid)['state'], 'VALIDATING')
        self.assertEqual(self.flow.execution_requests(), [])
        self.assertEqual(self.flow.store.db.execute('SELECT COUNT(*) FROM guard_reports').fetchone()[0], 0)

    def test_bound_report_commits_with_result_and_exact_replay(self):
        self.assertFalse(self.finish(report=self.report)['replay'])
        self.assertEqual(self.flow.store.action(self.flow.aid)['state'], 'READY')
        self.assertTrue(self.finish(report=self.report)['replay'])
        self.assertEqual(len(self.flow.execution_requests()), 1)
        saved = self.flow.store.db.execute('SELECT report FROM guard_reports').fetchone()[0]
        self.assertEqual(json.loads(saved), self.report)

    def test_changed_proof_is_rejected_before_and_after_commit(self):
        changed = deepcopy(self.report)
        changed['input_fingerprints']['dependencies'] = '0' * 64
        with self.assertRaises(ProtocolError):
            self.finish(report=changed)
        self.assertEqual(self.flow.store.db.execute('SELECT COUNT(*) FROM guard_reports').fetchone()[0], 0)
        self.finish(report=self.report)
        with self.assertRaises(ProtocolError):
            self.finish(report=changed)
        self.assertEqual(len(self.flow.execution_requests()), 1)

    def test_transaction_failure_keeps_report_result_and_ack_retryable(self):
        from unittest.mock import patch
        with patch('runtime.worker_transport.acknowledge', side_effect=RuntimeError('lost handoff')):
            with self.assertRaises(RuntimeError):
                self.finish(report=self.report)
        self.assertEqual(self.flow.store.action(self.flow.aid)['state'], 'VALIDATING')
        self.assertEqual(self.flow.store.db.execute('SELECT COUNT(*) FROM guard_reports').fetchone()[0], 0)
        self.assertFalse(self.finish(report=self.report)['replay'])

    def test_changed_evidence_retires_leased_request_and_delivers_latest_inputs(self):
        newer = dict(self.latest, checked_at=self.flow.time(43).isoformat().replace('+00:00', 'Z'))
        handle(newer, self.flow.store, self.flow.bus)
        replacement = self.delivery.claim('guard-2', 'guard')['delivery']
        self.assertIsNotNone(replacement)
        self.assertEqual(replacement['payload']['dependencies'], newer)
        self.assertNotEqual(replacement['delivery_id'], self.claim['delivery_id'])
        with self.assertRaises(ProtocolError) as error:
            self.finish(report=self.report)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.flow.store.action(self.flow.aid)['state'], 'VALIDATING')
        decision, report = evaluate_with_report(self.flow.proposal, self.flow.state,
            self.flow.policy, self.flow.baseline, newer, now=self.flow.time(44))
        answer = self.delivery.finish('guard-2', 'guard', replacement['delivery_id'],
                                     replacement['lease_token'], decision, report=report)
        self.assertEqual(answer, {'accepted': True, 'replay': False})
        self.assertEqual(self.flow.store.action(self.flow.aid)['state'], 'READY')

    def test_managed_approval_is_not_exposed_in_generic_worker_delivery(self):
        flow = self.flow
        flow.policy['require_operator_approval'] = True
        self.decision, self.report = evaluate_with_report(flow.proposal, flow.state,
            flow.policy, flow.baseline, self.latest, now=flow.time(42))
        self.finish(report=self.report)
        self.assertEqual(flow.store.action(flow.aid)['state'], 'AWAITING_APPROVAL')
        flow.store.clock = lambda: flow.time(44)
        record = approve(flow.aid, 'protected-operator', flow.store, managed=True,
            expires_at=flow.time(50).isoformat().replace('+00:00', 'Z'),
            expected_proposal_sha256=digest(flow.proposal),
            expected_policy_version=flow.policy['policy_version'])
        delivery = self.delivery.claim('guard-without-approval-acl', 'guard')['delivery']
        self.assertIsNone(delivery['payload']['approval'])
        persisted = flow.store.pending_routes('guard.request')[-1]
        self.assertEqual(json.loads(persisted['payload'])['approval'], record)

    def test_actual_http_handoff_preserves_report_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'coordinator.sqlite'
            with SQLiteStore(database) as persisted:
                self.flow.store.db.backup(persisted.db)
            token = 'local-test-guard-token-' + 'x' * 32
            with ThreadingHTTPServer(('127.0.0.1', 0), handler(database, {'guard-http': ('guard', token)})) as server:
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    client = WorkerClient(f'http://127.0.0.1:{server.server_port}', 'guard-http', token)
                    delivery = client.post('claim', {})['delivery']
                    body = {'delivery_id': delivery['delivery_id'], 'lease_token': delivery['lease_token'],
                            'result': self.decision, 'report': self.report}
                    self.assertFalse(client.post('result', body)['replay'])
                    self.assertTrue(client.post('result', body)['replay'])
                    with SQLiteStore(database) as persisted:
                        self.assertEqual(persisted.action(self.flow.aid)['state'], 'READY')
                        saved = json.loads(persisted.db.execute('SELECT report FROM guard_reports').fetchone()[0])
                        self.assertEqual(saved, self.report)
                finally:
                    server.shutdown()
                    worker.join(3)
