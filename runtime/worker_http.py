"""Run with python -m runtime.worker_http --help. Secrets are never logged."""
import argparse
import ipaddress
import json
import math
import signal
import socket
import ssl
import threading
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from runtime.service_identity import authenticate, load_identities
from runtime.worker_transport import Delivery, INGRESS, ProtocolError, TOPICS
from services.coordinator.core import SQLiteStore

MAX_BODY = 1024 * 1024


def identities(path):
    return load_identities(path, set(TOPICS) | set(INGRESS))


def handler(database, services, lease_seconds=30, retention_seconds=86400,
            request_timeout=15, stopping=None, clock=None, dependency_requests=False):
    class WorkerHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Application status belongs in structured logs, without request bodies.

        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)

        def do_GET(self):
            if self.path not in {"/healthz", "/readyz"}:
                self.reply(404, {"error": "Unknown endpoint"})
                return
            if stopping is not None and stopping.is_set():
                self.reply(503, {"ready": False})
                return
            if self.path == "/readyz":
                try:
                    with SQLiteStore(database, clock=clock) as store, store.transaction():
                        store.db.execute("SELECT 1 FROM outbox LIMIT 1").fetchone()
                except Exception:
                    self.reply(503, {"ready": False})
                    return
            self.reply(200, {"ready": True})

        def do_POST(self):
            try:
                if stopping is not None and stopping.is_set():
                    raise ProtocolError(503, "Coordinator is stopping; retry the saved request")
                name, role = authenticate(self.headers, services)
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ProtocolError(400, "Ambiguous request framing")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY:
                    raise ProtocolError(413, "Invalid body length")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ProtocolError(400, "Incomplete request body")
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise ProtocolError(400, "Expected JSON object")
                with SQLiteStore(database, clock=clock) as store:
                    delivery = Delivery(store, lease_seconds=lease_seconds, retention_seconds=retention_seconds,
                                        dependency_requests=dependency_requests)
                    if self.path == "/v1/claim":
                        answer = delivery.claim(name, role)
                    elif self.path == "/v1/ingest":
                        answer = delivery.ingest(role, body["message"])
                    elif self.path == "/v1/renew":
                        answer = delivery.renew(name, role, body["delivery_id"], body["lease_token"])
                    elif self.path == "/v1/dependencies":
                        answer = delivery.dependencies(role, body["proposal"])
                    elif self.path in ("/v1/result", "/v1/retry"):
                        answer = delivery.finish(name, role, body["delivery_id"], body["lease_token"],
                                                 body.get("result"), retry=self.path == "/v1/retry",
                                                 report=body.get("report"))
                    else:
                        raise ProtocolError(404, "Unknown endpoint")
                self.reply(200, answer)
            except ProtocolError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid input or lifecycle transition"})
            except (TimeoutError, socket.timeout):
                self.reply(408, {"error": "Request timed out"})
            except Exception:
                self.reply(503, {"error": "Coordinator unavailable; retry the saved request"})

        def reply(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ConnectionError, socket.timeout):
                # The transaction may already be committed. The worker must
                # replay its saved body to recover the durable receipt.
                self.close_connection = True
    return WorkerHandler


class WorkerClient:
    """One bounded POST. Callers retain the immutable result for explicit retry."""
    def __init__(self, url, identity, token, *, timeout=15, tls_context=None):
        self.url = urlsplit(url)
        if (not self.url.hostname or self.url.username is not None or self.url.password is not None
                or self.url.path not in {"", "/"} or self.url.query or self.url.fragment):
            raise ValueError("Expected a Coordinator origin URL without credentials, path, query or fragment")
        self.url.port  # Validate an explicitly supplied port before issuing requests.
        if self.url.scheme != "https" and not (self.url.scheme == "http" and
                self.url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Use HTTPS outside loopback")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        self.identity, self.token, self.timeout = identity, token, timeout
        self.tls_context = tls_context

    def post(self, operation, body):
        if operation not in {"claim", "ingest", "result", "retry", "renew", "dependencies"}:
            raise ValueError("Unknown operation")
        encoded = json.dumps(body, allow_nan=False).encode()
        if not isinstance(body, dict) or not 0 < len(encoded) <= MAX_BODY:
            raise ValueError("Request must be a JSON object of at most 1 MiB")
        if self.url.scheme == "https":
            connection = HTTPSConnection(self.url.hostname, self.url.port, timeout=self.timeout, context=self.tls_context)
        else:
            connection = HTTPConnection(self.url.hostname, self.url.port, timeout=self.timeout)
        try:
            connection.request("POST", "/v1/" + operation, encoded,
                {"Authorization": "Bearer " + self.token, "X-Service-Identity": self.identity,
                 "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ProtocolError(502, "Coordinator response exceeds 1 MiB")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError(502, "Coordinator returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise ProtocolError(502, "Coordinator returned a non-object response")
            if response.status != 200:
                raise ProtocolError(response.status, value.get("error", "Request failed"))
            return value
        finally:
            connection.close()


class WorkerHTTPServer(ThreadingHTTPServer):
    """Drain bounded in-flight requests when closed; never abandon a commit."""
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, request_handler, *, tls_context=None, request_timeout=15):
        self.tls_context, self.request_timeout = tls_context, request_timeout
        super().__init__(address, request_handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(self.request_timeout)
        if self.tls_context:
            try:
                connection = self.tls_context.wrap_socket(connection, server_side=True)
            except BaseException:
                connection.close()
                raise
        return connection, address


def serve_until_stopped(server, stopping):
    server.timeout = 0.25
    while not stopping.is_set():
        server.handle_request()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--identities", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--lease-seconds", type=int, default=30)
    parser.add_argument("--retention-seconds", type=int, default=86400)
    parser.add_argument("--request-timeout", type=float, default=15)
    args = parser.parse_args(argv)
    if bool(args.cert) != bool(args.key) or (not args.cert and not ipaddress.ip_address(args.bind).is_loopback):
        parser.error("Non-loopback serving requires --cert and --key")
    if (args.lease_seconds < 1 or args.retention_seconds < 1
            or not math.isfinite(args.request_timeout) or args.request_timeout <= 0):
        parser.error("Lease, retention and request timeout must be positive and finite")
    entries = identities(args.identities)
    with SQLiteStore(args.db) as store:
        Delivery(store, lease_seconds=args.lease_seconds, retention_seconds=args.retention_seconds)
    tls = None
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
    stopping = threading.Event()
    previous = {sig: signal.signal(sig, lambda signum, frame: stopping.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        with WorkerHTTPServer((args.bind, args.port),
                handler(args.db, entries, args.lease_seconds, args.retention_seconds,
                        args.request_timeout, stopping), tls_context=tls,
                request_timeout=args.request_timeout) as server:
            print(f"Coordinator worker API listening on {args.bind}:{args.port}", flush=True)
            serve_until_stopped(server, stopping)
    finally:
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
