import json
import io
from pathlib import Path
import tempfile
import threading
import unittest
from http.client import HTTPConnection, HTTPMessage
from http.server import ThreadingHTTPServer
from unittest.mock import Mock, patch

from runtime.worker_http import handler, WorkerClient, WorkerHTTPServer, serve_until_stopped
from runtime.worker_transport import Delivery, ProtocolError
from services.coordinator.core import SQLiteStore, LocalEventBus, handle
from shared.http import drain_request_body

ROOT = Path(__file__).resolve().parents[1]


def sample(name):
    return json.loads((ROOT / 'data/sentinel/samples' / 'messages' / (name + '.json')).read_text())


class RequestBodyDrainTests(unittest.TestCase):
    def test_drain_is_bounded_by_the_declared_length_and_cap(self):
        for size in (1, 4):
            with self.subTest(size=size):
                headers = HTTPMessage()
                headers['Content-Length'] = str(size)
                stream = io.BytesIO(b'bodyNEXT')
                request = Mock(headers=headers, rfile=Mock(wraps=stream))
                drain_request_body(request, 4)
                request.rfile.read.assert_called_once_with(size)
                self.assertEqual(stream.tell(), size)
                request.connection.settimeout.assert_not_called()

    def test_drain_skips_missing_invalid_oversize_or_ambiguous_lengths(self):
        cases = [[], [('Transfer-Encoding', 'chunked'), ('Content-Length', '2')],
                 [('Content-Length', '2'), ('Content-Length', '2')]]
        cases += [[('Content-Length', value)] for value in
                  ('', 'no', '-1', '0', '5', '2, 2', '+2', '1_0', '\u0662', '9' * 5000)]
        for values in cases:
            with self.subTest(headers=values):
                headers = HTTPMessage()
                for name, value in values:
                    headers[name] = value
                request = Mock(headers=headers)
                drain_request_body(request, 4)
                request.rfile.read.assert_not_called()

    def test_drain_does_not_retry_timeout_or_connection_failure(self):
        headers = HTTPMessage()
        headers['Content-Length'] = '2'
        for error in (TimeoutError, ConnectionAbortedError):
            with self.subTest(error=error):
                request = Mock(headers=headers)
                request.rfile.read.side_effect = error
                drain_request_body(request, 4)
                request.rfile.read.assert_called_once_with(2)


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

    def test_second_fault_waits_through_verification_and_other_workloads_keep_running(self):
        first = self.claim()
        second = dict(self.incident, incident_id="INC-SECOND-FAULT", observed_generation=3)
        independent = dict(self.incident, incident_id="INC-INDEPENDENT", target={
            **self.incident["target"], "name": "other-api", "uid": "other-uid"})
        self.worker.ingest("observer", second)
        self.worker.ingest("observer", independent)
        independent_work = self.claim("other-worker")
        self.assertEqual(independent_work["payload"]["incident"], independent)
        self.assertIsNone(self.claim("blocked-worker"))
        self.submit(first)
        bus = LocalEventBus()
        guard = sample("guard-result")
        guard.update(verdict="ALLOW", reason_codes=["VALIDATION_PASSED"])
        handle(guard, self.store, bus)
        handle(sample("execution-result"), self.store, bus)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "VERIFYING")
        self.assertIsNone(self.claim("blocked-worker"))
        outcome = sample("outcome")
        outcome["status"] = "NOT_RECOVERED"
        outcome["checks"][0]["passed"] = False
        handle(outcome, self.store, bus)
        # Queue state survives a Coordinator restart and releases after failure.
        self.store.close()
        self.open()
        queued = self.claim("next-worker")
        self.assertEqual(queued["payload"]["incident"], second)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "FAILED")

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

    def test_renew_keeps_long_work_owned_without_changing_delivery_or_attempt(self):
        d = self.claim()
        self.now += 4
        renewed = self.worker.renew('p', 'planner', d['delivery_id'], d['lease_token'])
        self.assertEqual(renewed['expires_at'], self.now + 5)
        self.now += 3
        self.assertIsNone(self.claim('replacement'))
        self.assertFalse(self.submit(d)['replay'])
        with self.assertRaises(ProtocolError):
            self.worker.renew('p', 'planner', d['delivery_id'], d['lease_token'])

    def test_renew_cannot_revive_expired_replaced_or_wrong_role_work(self):
        d = self.claim()
        for identity, role, token in (('other', 'planner', d['lease_token']),
                                      ('p', 'guard', d['lease_token']), ('p', 'planner', 'wrong')):
            with self.subTest(identity=identity, role=role, token=token), self.assertRaises(ProtocolError):
                self.worker.renew(identity, role, d['delivery_id'], token)
        self.now += 5
        with self.assertRaises(ProtocolError):
            self.worker.renew('p', 'planner', d['delivery_id'], d['lease_token'])
        self.now += 2
        newer = self.claim('replacement')
        with self.assertRaises(ProtocolError):
            self.worker.renew('p', 'planner', d['delivery_id'], d['lease_token'])
        self.assertEqual(newer['delivery_id'], d['delivery_id'])

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

    def test_early_http_refusals_drain_without_parsing_and_preserve_the_status(self):
        stopping = threading.Event()
        worker_handler = handler(self.path, {'p': ('planner', 'good')}, stopping=stopping)
        body = b'not JSON' + b'x' * (32 * 1024)
        with WorkerHTTPServer(('127.0.0.1', 0), worker_handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for status in (401, 503):
                    if status == 503:
                        stopping.set()
                    for attempt in range(3):
                        with self.subTest(status=status, attempt=attempt):
                            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                            try:
                                connection.request('POST', '/v1/claim', body=body,
                                                   headers={'X-Service-Identity': 'p', 'Authorization': 'Bearer bad'})
                                response = connection.getresponse()  # The entire body has already been sent.
                                self.assertEqual(response.status, status)
                                response.read()
                            finally:
                                connection.close()
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM transport_receipts').fetchone()[0], 0)
            finally:
                server.shutdown()
                thread.join(3)

    def test_worker_does_not_drain_again_after_a_body_read_times_out(self):
        request = object.__new__(handler(self.path, {'p': ('planner', 'good')}))
        request.path = '/v1/claim'
        request.headers = HTTPMessage()
        for name, value in [('Content-Length', '2'), ('Authorization', 'Bearer good'), ('X-Service-Identity', 'p')]:
            request.headers[name] = value
        request.rfile = Mock(read=Mock(side_effect=TimeoutError))
        request.reply = Mock()
        request.do_POST()
        request.rfile.read.assert_called_once_with(2)
        self.assertEqual(request.reply.call_args.args[0], 408)

    def test_two_connections_cannot_claim_the_same_active_lease(self):
        barrier = threading.Barrier(2)
        claims, errors = [], []

        def claim(identity):
            try:
                with SQLiteStore(self.path) as store:
                    worker = Delivery(store, clock=lambda: self.now, lease_seconds=5)
                    barrier.wait(timeout=3)
                    claims.append(worker.claim(identity, 'planner')['delivery'])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=claim, args=(name,)) for name in ('p1', 'p2')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_duplicate_identity_or_authorization_headers_are_rejected(self):
        with WorkerHTTPServer(('127.0.0.1', 0), handler(self.path, {'p': ('planner', 'good')})) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for duplicate in ('Authorization', 'X-Service-Identity'):
                    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
                    try:
                        connection.putrequest('POST', '/v1/claim')
                        headers = {'Authorization': 'Bearer good', 'X-Service-Identity': 'p', 'Content-Length': '2'}
                        for name, value in headers.items():
                            connection.putheader(name, value)
                            if name == duplicate:
                                connection.putheader(name, value)
                        connection.endheaders(b'{}')
                        response = connection.getresponse()
                        self.assertEqual(response.status, 401)
                        response.read()
                    finally:
                        connection.close()
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM transport_receipts').fetchone()[0], 0)
            finally:
                server.shutdown()
                thread.join(3)

    def test_socket_timeout_after_commit_replays_the_saved_result(self):
        committed, release = threading.Event(), threading.Event()
        original = Delivery.finish

        def paused(worker, *args, **kwargs):
            answer = original(worker, *args, **kwargs)
            if not answer.get('replay'):
                committed.set()
                release.wait(3)
            return answer

        with WorkerHTTPServer(('127.0.0.1', 0), handler(self.path, {'p': ('planner', 'good')})) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                client = WorkerClient(f'http://127.0.0.1:{server.server_port}', 'p', 'good')
                delivery = client.post('claim', {})['delivery']
                body = {'delivery_id': delivery['delivery_id'], 'lease_token': delivery['lease_token'],
                        'result': self.proposal}
                with patch.object(Delivery, 'finish', paused):
                    with self.assertRaises(TimeoutError):
                        WorkerClient(f'http://127.0.0.1:{server.server_port}', 'p', 'good', timeout=0.1).post('result', body)
                    self.assertTrue(committed.wait(1))
                    release.set()
                    self.assertTrue(client.post('result', body)['replay'])
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM actions').fetchone()[0], 1)
                self.assertIsNone(client.post('claim', {})['delivery'])
            finally:
                release.set()
                server.shutdown()
                thread.join(3)

    def test_health_readiness_and_shutdown_fence(self):
        stopping = threading.Event()
        with WorkerHTTPServer(('127.0.0.1', 0), handler(self.path, {'p': ('planner', 'good')}, stopping=stopping)) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                for path in ('/healthz', '/readyz'):
                    connection.request('GET', path)
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read()), {'ready': True})
                stopping.set()
                connection.request('GET', '/readyz')
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                response.read()
                with self.assertRaises(ProtocolError) as error:
                    WorkerClient(f'http://127.0.0.1:{server.server_port}', 'p', 'good').post('claim', {})
                self.assertEqual(error.exception.status, 503)
                self.assertEqual(self.store.db.execute('SELECT count(*) FROM transport_receipts').fetchone()[0], 0)
            finally:
                connection.close()
                server.shutdown()
                thread.join(3)

    def test_readiness_refuses_unavailable_storage(self):
        with WorkerHTTPServer(('127.0.0.1', 0), handler(self.path.parent / 'missing' / 'db', {})) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=2)
            try:
                connection.request('GET', '/readyz')
                response = connection.getresponse()
                self.assertEqual(response.status, 503)
                self.assertEqual(json.loads(response.read()), {'ready': False})
            finally:
                connection.close()
                server.shutdown()
                thread.join(3)

    def test_graceful_loop_waits_for_an_inflight_commit(self):
        entered, release, stopping, closed = (threading.Event() for _ in range(4))
        original = Delivery.finish

        def paused(worker, *args, **kwargs):
            entered.set()
            release.wait(3)
            return original(worker, *args, **kwargs)

        server = WorkerHTTPServer(('127.0.0.1', 0), handler(self.path, {'p': ('planner', 'good')}, stopping=stopping))

        def serve():
            with server:
                serve_until_stopped(server, stopping)
            closed.set()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client = WorkerClient(f'http://127.0.0.1:{server.server_port}', 'p', 'good')
        delivery = client.post('claim', {})['delivery']
        body = {'delivery_id': delivery['delivery_id'], 'lease_token': delivery['lease_token'], 'result': self.proposal}
        answers = []
        sender = threading.Thread(target=lambda: answers.append(client.post('result', body)))
        try:
            with patch.object(Delivery, 'finish', paused):
                sender.start()
                self.assertTrue(entered.wait(2))
                stopping.set()
                self.assertFalse(closed.wait(0.4))
                release.set()
                sender.join(3)
                thread.join(3)
                self.assertEqual(answers, [{'accepted': True, 'replay': False}])
                self.assertTrue(closed.is_set())
                self.assertIsNotNone(self.store.action(self.proposal['action_id']))
        finally:
            release.set()
            stopping.set()
            if sender.ident is not None:
                sender.join(3)
            thread.join(3)

    def test_client_rejects_ambiguous_urls_and_unbounded_requests(self):
        for url in ('http://service:8766', 'https://user:secret@service', 'https://service/path',
                    'https://service?key=secret', 'https://service#fragment', 'https://service:invalid', 'https://'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                WorkerClient(url, 'p', 'good')
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkerClient('http://127.0.0.1', 'p', 'good', timeout=value)
            with self.assertRaises(ValueError):
                Delivery(self.store, lease_seconds=value)
        client = WorkerClient('http://127.0.0.1', 'p', 'good')
        for body in ([], {'huge': 'x' * (1024 * 1024)}, {'nonfinite': float('nan')}):
            with self.subTest(body_type=type(body)), self.assertRaises(ValueError):
                client.post('claim', body)
