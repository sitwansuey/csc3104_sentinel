import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

from runtime.worker_http import handler, WorkerClient
from runtime.worker_transport import Delivery, ProtocolError
from services.coordinator.core import SQLiteStore

ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / 'data/sentinel/samples' / 'messages' / (name + '.json')).read_text())


class TransportTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'coordinator.db'
        self.now = 1
        self.open()
        self.addCleanup(lambda: self.store.close())
        self.incident, self.proposal = sample('incident'), sample('proposal')
        self.worker.ingest('observer', self.incident)

    def open(self):
        self.store = SQLiteStore(self.path)
        self.worker = Delivery(self.store, clock=lambda: self.now, lease_seconds=5, retention_seconds=20)

    def claim(self, name='p'):
        return self.worker.claim(name, 'planner')['delivery']

    def submit(self, delivery, proposal=None, identity='p'):
        return self.worker.finish(identity, 'planner', delivery['delivery_id'], delivery['lease_token'],
                                  self.proposal if proposal is None else proposal)

    def test_restart_lost_claim_response_and_old_lease(self):
        first = self.claim()
        self.store.close()
        self.open()
        self.assertIsNone(self.claim('replacement'))
        self.now += 7
        second = self.claim('replacement')
        self.assertEqual(first['delivery_id'], second['delivery_id'])
        self.assertEqual(first['payload'], second['payload'])
        self.assertNotEqual(first['lease_token'], second['lease_token'])
        with self.assertRaises(ProtocolError):
            self.submit(first)
        self.submit(second, identity='replacement')

    def test_lost_result_response_recovers_exact_receipt_after_restart(self):
        d = self.claim()
        self.assertFalse(self.submit(d)['replay'])
        self.store.close()
        self.open()
        self.assertTrue(self.submit(d)['replay'])
        with self.assertRaises(ProtocolError):
            self.submit(d, dict(self.proposal, rationale='altered'))
        self.assertIsNone(self.claim())
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM actions').fetchone()[0], 1)
        self.assertIsNotNone(self.worker.claim('g', 'guard')['delivery'])

    def test_timeout_backoff_and_role_fences(self):
        d = self.claim()
        with self.assertRaises(ProtocolError):
            self.worker.ingest('planner', self.incident)
        with self.assertRaises(ProtocolError):
            self.worker.claim('obs', 'observer')
        with self.assertRaises(ProtocolError):
            self.submit(d, identity='other')
        with self.assertRaises(ProtocolError):
            self.submit(d, dict(self.proposal, incident_id='wrong'))
        with self.assertRaises(ProtocolError):
            self.worker.finish('p','guard',d['delivery_id'],d['lease_token'], self.proposal)
        self.worker.finish('p','planner',d['delivery_id'],d['lease_token'],None,retry=True)
        self.assertIsNone(self.claim())
        self.now += 1
        replacement = self.claim()
        self.now += 5
        with self.assertRaises(ProtocolError):
            self.submit(replacement)

    def test_failed_commit_leaves_result_and_ack_retryable(self):
        d = self.claim()
        with patch('runtime.worker_transport.acknowledge', side_effect=RuntimeError()):
            with self.assertRaises(RuntimeError):
                self.submit(d)
        self.assertIsNone(self.store.action(self.proposal['action_id']))
        self.submit(d)

    def test_receipts_expire_without_forgetting_business_ids(self):
        d = self.claim()
        self.submit(d)
        self.now += 21
        with self.assertRaises(ProtocolError) as error:
            self.submit(d)
        self.assertEqual(error.exception.status, 404)
        self.assertTrue(self.worker.ingest('observer', self.incident)['replay'])
        self.assertIsNone(self.claim())

    def test_http_claim_result_and_authentication(self):
        with ThreadingHTTPServer(('127.0.0.1', 0), handler(self.path, {'planner1':('planner','good')})) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_port)
                with self.assertRaises(ProtocolError) as error:
                    WorkerClient(url,'planner1','bad').post('claim',{})
                self.assertEqual(error.exception.status,401)
                client = WorkerClient(url,'planner1','good')
                with self.assertRaises(ProtocolError) as forbidden:
                    client.post('ingest',{'message':self.incident})
                self.assertEqual(forbidden.exception.status,403)
                d = client.post('claim',{})['delivery']
                request = {'delivery_id':d['delivery_id'],'lease_token':d['lease_token'],'result':self.proposal}
                self.assertFalse(client.post('result',request)['replay'])
                self.assertTrue(client.post('result',request)['replay'])
            finally:
                server.shutdown()
                thread.join()
