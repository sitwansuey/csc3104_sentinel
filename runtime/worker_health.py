"""Liveness and readiness of one worker process, served on loopback only.

/healthz answers 200 while the polling loop is not stopping and started a pass
within the liveness window, so a loop stuck inside one pass (a hung inference
or blocked I/O) turns unhealthy once the window passes.
/readyz answers 200 only when the worker's configuration was accepted and the
Coordinator answered a polling pass within the readiness window. Both bodies
hold booleans and ages in seconds only, never configuration or credentials.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time


class WorkerHealth:
    """Monotonic timestamps of the loop heartbeat and the last Coordinator answer."""

    def __init__(self, *, ready_seconds=60, live_seconds=900, clock=time.monotonic):
        self.ready_seconds, self.live_seconds, self.clock = ready_seconds, live_seconds, clock
        self._configured = False
        self._beat = None
        self._contact = None

    def configured(self):
        self._configured = True

    def heartbeat(self):
        self._beat = self.clock()

    def contacted(self):
        self._contact = self.clock()

    def _age(self, stamp):
        return None if stamp is None else round(self.clock() - stamp, 3)

    def live(self, stopping=None):
        age = self._age(self._beat)
        alive = age is not None and age <= self.live_seconds and not (stopping is not None and stopping.is_set())
        return (200 if alive else 503), {"alive": alive, "loop_age_seconds": age}

    def ready(self, stopping=None):
        age = self._age(self._contact)
        ready = (self._configured and age is not None and age <= self.ready_seconds
                 and not (stopping is not None and stopping.is_set()))
        return (200 if ready else 503), {"ready": ready, "contact_age_seconds": age}


def handler(health, stopping):
    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Probes are frequent; status belongs in the structured worker log.

        def do_GET(self):
            if self.path == "/healthz":
                status, body = health.live(stopping)
            elif self.path == "/readyz":
                status, body = health.ready(stopping)
            else:
                status, body = 404, {"error": "Unknown endpoint"}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return HealthHandler


@contextmanager
def serve_health(health, port, stopping=None):
    """Serve the probes on 127.0.0.1:port in a daemon thread; yield the server."""
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(health, stopping))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
