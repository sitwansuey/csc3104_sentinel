"""Run with python -m runtime.worker_http --help. Secrets are never logged."""
import argparse
import ipaddress
import json
import math
import signal
import socket
import re
import ssl
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from runtime.service_identity import authenticate, load_identities
from runtime.worker_transport import Delivery, INGRESS, ProtocolError, TOPICS, delivery_ids
from services.coordinator.core import SQLiteStore
from shared import log

MAX_BODY = 1024 * 1024
LOG = log.get_logger("worker_http")
OPERATIONS = {"/v1/" + name: name for name in ("claim", "ingest", "renew", "dependencies", "result", "retry")}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# Denial and conflict reason classes. Messages are fixed strings from the
# delivery protocol; anything unlisted falls back to a per-status class.
REASONS = {
    "Invalid credentials": "invalid_credentials",
    "Role cannot claim requests": "role_not_permitted",
    "Role cannot retrieve authorization evidence": "role_not_permitted",
    "Role cannot submit this message": "role_not_permitted",
    "Only the Guard may submit an authorization report": "role_not_permitted",
    "Delivery belongs to another worker": "not_lease_owner",
    "Delivery retired or lease expired/replaced": "lease_not_current",
    "Lease has been replaced": "lease_replaced",
    "Lease expired": "lease_expired",
    "Delivery retired or result changed": "result_changed",
    "Result does not correspond to claimed request": "result_mismatch",
    "Guard result needs its report bound to current evidence": "report_not_bound",
    "Guard report conflicts with stored evidence": "report_conflict",
    "Evidence does not match the active proposal": "evidence_not_current",
}
DEFAULT_REASONS = {401: "invalid_credentials", 403: "forbidden", 409: "conflict"}
OUTCOMES = {400: "invalid", 401: "denied", 403: "denied", 404: "not_found", 408: "timeout",
            409: "conflict", 413: "invalid", 503: "unavailable"}


def identities(path):
    return load_identities(path, set(TOPICS) | set(INGRESS))


def safe_id(value):
    """Return an identifier fit for a log line, or None. Never pass tokens here."""
    return value if isinstance(value, str) and SAFE_ID.fullmatch(value) else (
        value if type(value) is int else None)


def reason_class(status, message, reasons=REASONS):
    if status not in DEFAULT_REASONS:
        return None
    return reasons.get(message, DEFAULT_REASONS[status])


def access_log(logger, operation, status, started, *, outcome=None, reason=None, error_type=None, **ids):
    """One line per request: no headers, tokens or bodies, only IDs and codes."""
    level = (log.ERROR if status >= 500 else log.WARNING if status >= 400
             else log.DEBUG if outcome in {"idle", "renewed"} else log.INFO)
    log.emit(logger, "auth.denied" if status in {401, 403} else "http.access", level=level,
             method="POST", operation=operation, http_status=status,
             outcome=outcome or OUTCOMES.get(status, "ok" if status < 400 else "error"),
             reason=reason, error_type=error_type,
             duration_ms=round((time.monotonic() - started) * 1000, 1),
             **{key: safe_id(value) for key, value in ids.items()})


def _message_ids(message):
    return ({"incident_id": message.get("incident_id"), "action_id": message.get("action_id")}
            if isinstance(message, dict) else {})


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
            started, operation = time.monotonic(), OPERATIONS.get(self.path, "unknown")
            ids, outcome, reason, error_type = {}, None, None, None
            try:
                if stopping is not None and stopping.is_set():
                    raise ProtocolError(503, "Coordinator is stopping; retry the saved request")
                name, role = authenticate(self.headers, services)
                ids["identity"] = name
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
                # IDs only: the lease token and the body itself are never logged.
                ids["delivery_id"] = body.get("delivery_id")
                ids.update(_message_ids(body.get("message") or body.get("result") or body.get("proposal")))
                with SQLiteStore(database, clock=clock) as store:
                    delivery = Delivery(store, lease_seconds=lease_seconds, retention_seconds=retention_seconds,
                                        dependency_requests=dependency_requests)
                    if self.path == "/v1/claim":
                        answer = delivery.claim(name, role)
                        ids.update(delivery_ids(answer["delivery"]))
                        outcome = "claimed" if answer["delivery"] else "idle"
                    elif self.path == "/v1/ingest":
                        answer = delivery.ingest(role, body["message"])
                        outcome = "replay" if answer.get("replay") else "accepted"
                    elif self.path == "/v1/renew":
                        answer = delivery.renew(name, role, body["delivery_id"], body["lease_token"])
                        outcome = "renewed"
                    elif self.path == "/v1/dependencies":
                        answer = delivery.dependencies(role, body["proposal"])
                    elif self.path in ("/v1/result", "/v1/retry"):
                        answer = delivery.finish(name, role, body["delivery_id"], body["lease_token"],
                                                 body.get("result"), retry=self.path == "/v1/retry",
                                                 report=body.get("report"))
                        outcome = ("retry" if self.path == "/v1/retry"
                                   else "replay" if answer.get("replay") else "accepted")
                    else:
                        raise ProtocolError(404, "Unknown endpoint")
                status, response = 200, answer
            except ProtocolError as exc:
                status, reason, response = exc.status, reason_class(exc.status, str(exc)), {"error": str(exc)}
            except (ValueError, KeyError, TypeError) as exc:
                status, error_type = 400, type(exc).__name__
                response = {"error": "Invalid input or lifecycle transition"}
            except (TimeoutError, socket.timeout):
                status, response = 408, {"error": "Request timed out"}
            except Exception as exc:
                status, error_type = 503, type(exc).__name__
                response = {"error": "Coordinator unavailable; retry the saved request"}
            # Log before replying, so the line exists once the client sees the answer.
            access_log(LOG, operation, status, started, outcome=outcome, reason=reason,
                       error_type=error_type, **ids)
            self.reply(status, response)

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


@log.entrypoint("worker_http")
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
            log.emit(LOG, "worker_http.listening", port=server.server_port)
            serve_until_stopped(server, stopping)
    finally:
        for sig, previous_handler in previous.items():
            signal.signal(sig, previous_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
