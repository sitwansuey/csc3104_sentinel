"""Envoy HTTP ext_authz endpoint; private loopback sidecar, no cluster credentials.

Capacity is bounded (P5.04): at most max_inflight inspections run at once
(a non-blocking semaphore) and at most max_rps start per second (a token
bucket). A request over either bound is answered 503 at once, and Envoy's
ext_authz keeps failure_mode_allow false, so overload denies a request; it
never lets one pass uninspected. GET /readyz answers 200 only while the
journal takes a write and a slot is free, so the gateway Pod leaves its
Services' endpoints while the inspector could only deny.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sqlite3
import threading
import time

from runtime.security_store import probe_writable, record
from services.grpc_inspection import MAX_BODY, ROUTE_METHODS, inspect_request
from shared import log

LOG = log.get_logger("security_inspector")
MAX_INFLIGHT = 16
MAX_RPS = 200.0


class Capacity:
    """A non-blocking inflight bound plus a token bucket; thread-safe, never waits."""

    def __init__(self, max_inflight=MAX_INFLIGHT, max_rps=MAX_RPS, *, clock=time.monotonic):
        if type(max_inflight) is not int or max_inflight < 1:
            raise ValueError("max_inflight must be a positive integer")
        if isinstance(max_rps, bool) or not isinstance(max_rps, (int, float)) or not 0 < max_rps <= 100000:
            raise ValueError("max_rps must be positive")
        self.max_inflight, self.rate, self.clock = max_inflight, float(max_rps), clock
        # One second of traffic may arrive at once; never less than one request.
        self.burst = max(1.0, self.rate)
        self._slots = threading.BoundedSemaphore(max_inflight)
        self._lock = threading.Lock()
        self._tokens, self._stamp = self.burst, clock()
        self.inflight = 0
        self.shed = {"inflight": 0, "rate": 0}

    def admit(self):
        """None when admitted (call release() afterwards), else why it was refused."""
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self.shed["inflight"] += 1
            return "inflight"
        with self._lock:
            now = self.clock()
            self._tokens = min(self.burst, self._tokens + max(0.0, now - self._stamp) * self.rate)
            self._stamp = now
            if self._tokens < 1:
                self.shed["rate"] += 1
                refused = "rate"
            else:
                self._tokens -= 1
                self.inflight += 1
                refused = None
        if refused:
            self._slots.release()
        return refused

    def release(self):
        with self._lock:
            self.inflight -= 1
        self._slots.release()

    def state(self):
        with self._lock:
            return {"inflight": self.inflight, "max_inflight": self.max_inflight, "max_rps": self.rate,
                    "shed": dict(self.shed)}


def readiness(journal, capacity):
    """(ready, reasons, capacity state): the journal takes a write and a slot is free."""
    reasons = []
    state = capacity.state()
    if state["inflight"] >= state["max_inflight"]:
        reasons.append("inflight_exhausted")
    try:
        probe_writable(journal)
    except (OSError, sqlite3.Error):
        reasons.append("journal_unwritable")
    return not reasons, reasons, state


class _Server(ThreadingHTTPServer):
    # The default listen backlog (5) refuses connections in a burst before the
    # capacity check can answer 503. Envoy holds at most INSPECTOR_CONNECTIONS
    # (32) to the inspector, so this backlog covers every one of them.
    request_queue_size = 64
    daemon_threads = True


def make_server(journal, *, port=9001, stdout_events=False, max_inflight=MAX_INFLIGHT, max_rps=MAX_RPS,
                clock=time.monotonic):
    capacity = Capacity(max_inflight, max_rps, clock=clock)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SentinelInspector"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_GET(self):
            if self.path != "/readyz":
                self.send_error(405)
                return
            ready, reasons, state = readiness(journal, capacity)
            body = json.dumps({"ready": ready, "reasons": reasons, **state}, allow_nan=False).encode()
            self.send_response(200 if ready else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            try:
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
                    raise ValueError("Invalid framing")
                size = int(lengths[0])
                if self.headers.get("Transfer-Encoding") or not 0 <= size <= MAX_BODY:
                    raise ValueError("Invalid framing")
                body = self.rfile.read(size)
                if len(body) != size:
                    raise ValueError("Incomplete body")
                if (self.headers.get("Content-Type") != "application/octet-stream"
                        or self.headers.get("x-sentinel-body-format") != "grpc"):
                    raise ValueError("Expected the private HTTP authorization transport")
                # Envoy names the protected route; clients cannot set it (only
                # grpc-encoding is forwarded from the client's headers).
                routes = self.headers.get_all("x-sentinel-route", [])
                if len(routes) != 1 or routes[0] not in ROUTE_METHODS:
                    raise ValueError("Expected exactly one protected route")
            except (ValueError, TimeoutError):
                self.send_error(400)
                return
            # The body has been read, so an answer here never aborts the
            # client mid-send. Over capacity, deny now; never queue or pass.
            if capacity.admit():
                self.send_error(503)
                return
            try:
                result = inspect_request(self.path, "application/grpc",
                                         self.headers.get("grpc-encoding", ""), body, route=routes[0],
                                         partial=self.headers.get("x-envoy-auth-partial-body", "false") != "false")
                # Fail closed if the audit journal cannot be written.
                event = record(journal, "inspection", result)
                if stdout_events:
                    print(json.dumps(event, allow_nan=False), flush=True)
            except (ValueError, TimeoutError):
                self.send_error(400)
                return
            except OSError:
                self.send_error(503)
                return
            except Exception as error:  # noqa: BLE001 - fail closed: any other inspection error answers 503
                log.failure(LOG, "inspector.request.failed", error, level=log.ERROR)
                self.send_error(503)
                return
            finally:
                capacity.release()
            self.send_response(200 if result["verdict"] == "ALLOW" else 403)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, *args):
            pass

    # Share the proxy's network namespace. Never expose this port to clients.
    server = _Server(("127.0.0.1", port), Handler)
    server.capacity = capacity
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--stdout-events", action="store_true", help="Emit the same metadata-only audit records for log collection")
    parser.add_argument("--max-inflight", type=int, default=MAX_INFLIGHT,
                        help="Inspections at once; more are denied with 503 (default %(default)s)")
    parser.add_argument("--max-rps", type=float, default=MAX_RPS,
                        help="Inspections started per second; more are denied with 503 (default %(default)s)")
    args = parser.parse_args()
    with make_server(args.journal, port=args.port, stdout_events=args.stdout_events,
                     max_inflight=args.max_inflight, max_rps=args.max_rps) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
