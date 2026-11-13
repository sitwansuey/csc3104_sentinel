"""Test-only Kubernetes API server over real HTTP, for the Executor's REST transport (P5.08).

FakeAPIServer is a ThreadingHTTPServer on 127.0.0.1 in front of a
SimAPIServer, so it shares that simulator's documented semantics (RFC 6902
test, add and replace applied atomically, one monotonic resourceVersion, an
admission check that can deny a patch that applied cleanly) and the kubectl
stand-in SimKubectl can drive the very same object. It serves only what the
REST adapter sends:

  GET    /apis/apps/v1/namespaces/{namespace}/deployments/{name}
  PATCH  the same path, Content-Type application/json-patch+json
  GET    /apis/autoscaling/v2/namespaces/{namespace}/horizontalpodautoscalers

A failed test operation or an admission denial is a 422 Status with reason
Invalid, as a ValidatingAdmissionPolicy's default denial is; a missing object
is a 404 NotFound; a wrong bearer token is a 401. Every Status message starts
with PRIVATE, so a test can show that no server text is echoed.
fail_next_patch(mode) injects one fault into the next PATCH:

  bump-first          another writer commits first, so the patch's test fails (422)
  delete-first        the Deployment is deleted first (404)
  forbidden           403 Forbidden and nothing applied (an RBAC or webhook denial)
  status=<code>       answer <code> with a Status body and apply nothing
  commit-then-drop    the patch commits, then the connection closes with no response
  drop-before-commit  the connection closes with no response and nothing applied
  error-after-commit  the patch commits, then a 500 InternalError
  delay-then-commit   wait `delay` seconds (past the client's timeout), then commit

Nothing here contacts a cluster.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

from tests.support.sim_apiserver import is_fence


PRIVATE = "PRIVATE server detail"
_REASONS = {400: "BadRequest", 401: "Unauthorized", 403: "Forbidden", 404: "NotFound", 408: "Timeout",
            409: "Conflict", 415: "UnsupportedMediaType", 422: "Invalid", 429: "TooManyRequests",
            500: "InternalError", 502: "Unknown", 503: "ServiceUnavailable", 504: "Timeout"}


def bump(deployment):
    """Another writer's harmless change: it moves the resourceVersion."""
    deployment["metadata"].setdefault("annotations", {})["example.org/bump"] = str(time.monotonic_ns())


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002 - the base class names it so
        """Keep test output quiet."""

    @property
    def fake(self):
        return self.server.fake

    def _send(self, status, document):
        body = json.dumps(document).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # The client already gave up (delay-then-commit); the commit stands.
        self.close_connection = True

    def _status(self, code, *, kind="deployments"):
        reason = _REASONS.get(code, "Unknown")
        self._send(code, {"kind": "Status", "apiVersion": "v1", "metadata": {}, "status": "Failure",
                          "message": PRIVATE + ": " + reason, "reason": reason,
                          "details": {"name": self.fake.name, "group": "apps", "kind": kind}, "code": code})

    def _route(self, method):
        parts = urlsplit(self.path)
        self.fake.record(method, parts.path, parse_qs(parts.query), dict(self.headers))
        if self.fake.token is not None and self.headers.get("Authorization") != "Bearer " + self.fake.token:
            return self._status(401)
        deployment = f"/apis/apps/v1/namespaces/{self.fake.namespace}/deployments/{self.fake.name}"
        if method == "GET" and parts.path == deployment:
            return self._get()
        if method == "GET" and parts.path == f"/apis/autoscaling/v2/namespaces/{self.fake.namespace}/horizontalpodautoscalers":
            with self.fake.lock:
                items = json.loads(json.dumps(self.fake.sim.autoscalers))
            return self._send(200, {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscalerList",
                                    "items": items})
        if method == "PATCH" and parts.path == deployment:
            return self._patch()
        return self._status(405 if parts.path == deployment else 404, kind="unknown")

    def _get(self):
        with self.fake.lock:
            if not self.fake.sim.present:
                return self._status(404)
            document = json.loads(json.dumps(self.fake.sim.deployment))
        return self._send(200, document)

    def _patch(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if self.headers.get("Content-Type") != "application/json-patch+json":
            return self._status(415)
        try:
            operations = json.loads(body)
        except ValueError:
            return self._status(400)
        fault, delay = self.fake.next_fault()
        if fault == "forbidden":
            return self._status(403)
        if fault.startswith("status="):
            return self._status(int(fault.removeprefix("status=")))
        if fault == "drop-before-commit":
            self.close_connection = True
            return None
        with self.fake.lock:
            if fault == "bump-first":
                self.fake.sim.external_write(bump)
            elif fault == "delete-first":
                self.fake.sim.delete()
        if fault == "delay-then-commit":
            time.sleep(delay)
        with self.fake.lock:
            if not self.fake.sim.present:
                return self._status(404)
            request = self.fake.sim.submit("rest", "unknown", operations,
                                           kind="fence" if is_fence(operations) else "write")
            committed = self.fake.sim.deliver(request)
            document = json.loads(json.dumps(self.fake.sim.deployment))
        if committed:
            self.fake.landed.set()
        if fault == "commit-then-drop":
            self.close_connection = True
            return None
        if fault == "error-after-commit":
            return self._status(500)
        if not committed:
            return self._status(422)
        return self._send(200, document)

    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler dispatches by this name
        self._route("GET")

    def do_PATCH(self):  # noqa: N802
        self._route("PATCH")

    def do_PUT(self):  # noqa: N802
        self._route("PUT")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")


class FakeAPIServer:
    """One Deployment over HTTP (or HTTPS with tls_context), backed by a shared SimAPIServer.

    The Deployment's namespace and name are read from the simulator at each
    request (its last stored object while it is deleted), so the server can
    start before the object exists.
    """

    def __init__(self, sim, *, token=None, tls_context=None):
        self.sim, self.token = sim, token
        self.lock = threading.RLock()
        self.requests = []
        self.faults = []
        self.landed = threading.Event()  # Set whenever a PATCH commits, also after the client gave up.
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.fake = self
        self.scheme = "http"
        if tls_context is not None:
            self.httpd.socket = tls_context.wrap_socket(self.httpd.socket, server_side=True)
            self.scheme = "https"
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05},
                                        daemon=True)

    def _metadata(self):
        with self.lock:
            stored = self.sim.deployment if self.sim.deployment is not None else self.sim.last
            return stored["metadata"] if stored is not None else {"namespace": None, "name": None}

    @property
    def namespace(self):
        return self._metadata()["namespace"]

    @property
    def name(self):
        return self._metadata()["name"]

    @property
    def url(self):
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        return self

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.close()

    def record(self, method, path, query, headers):
        with self.lock:
            self.requests.append({"method": method, "path": path, "query": query, "headers": headers})

    def count(self, method):
        with self.lock:
            return sum(1 for request in self.requests if request["method"] == method)

    def fail_next_patch(self, mode, *, delay=0.5):
        with self.lock:
            self.faults.append((mode, delay))

    def next_fault(self):
        with self.lock:
            return self.faults.pop(0) if self.faults else ("", 0)
