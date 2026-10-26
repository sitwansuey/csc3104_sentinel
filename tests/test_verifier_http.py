from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import unittest
from urllib.error import URLError

from services.verifier_http import EvidenceHTTP, EvidenceUnavailable, validate_url


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        code = 503 if self.path == "/unhealthy" else 302 if self.path == "/redirect" else 200
        self.send_response(code)
        if code == 302:
            self.send_header("Location", "/healthy")
        self.end_headers()
        self.wfile.write(b"x" * 100 if self.path == "/large" else b'{"status":"ok"}')

    def log_message(self, *args):
        pass


class EvidenceHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_real_endpoint_success_and_failure_are_distinguished(self):
        http = EvidenceHTTP()
        self.assertEqual(http.health(self.base + "/healthy")["http_status"], 200)
        self.assertEqual(http.health(self.base + "/unhealthy")["http_status"], 503)

    def test_redirect_and_oversized_body_are_unknown(self):
        self.assertIsNone(EvidenceHTTP().health(self.base + "/redirect")["http_status"])
        self.assertIsNone(EvidenceHTTP(max_bytes=20).health(self.base + "/large")["http_status"])

    def test_transport_failure_does_not_fabricate_health(self):
        def unavailable(request, *, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(timeout, 2)
            raise URLError("unavailable")
        result = EvidenceHTTP(timeout_seconds=2, opener=unavailable).health(self.base)
        self.assertIsNone(result["http_status"])
        self.assertIsNotNone(result["error"])

    def test_trace_json_uses_real_endpoint(self):
        self.assertEqual(EvidenceHTTP().json(self.base), {"status": "ok"})
        with self.assertRaises(EvidenceUnavailable):
            EvidenceHTTP().json(self.base + "/unhealthy")

    def test_invalid_configuration_is_rejected(self):
        for url in ("file:///etc/passwd", "https://user:secret@example.org", "http://example.org/#fragment", "http://x:bad", None, 123):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        for timeout in (True, 0, 11, float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                EvidenceHTTP(timeout_seconds=timeout)
