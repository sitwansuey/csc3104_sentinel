"""Run with python -m runtime.worker_http --help. Secrets are never logged."""
import argparse
from collections import deque
from contextlib import ExitStack
from datetime import datetime, UTC
import ipaddress
import json
import math
import signal
import socket
import re
import sqlite3
import ssl
import threading
import time
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from runtime import fault_points
from runtime.service_identity import authenticate, load_identities
from runtime.worker_transport import (
    Delivery, INGRESS, ProtocolError, TOPICS, delivery_ids, parse_clock_estimate, within_skew_bound,
)
from services.coordinator.core import SQLiteStore
from shared import log, tracing

MAX_BODY = 1024 * 1024
LOG = log.get_logger("worker_http")
OPERATIONS = {"/v1/" + name: name for name in ("claim", "ingest", "renew", "dependencies", "result", "retry", "clock")}
# Cristian's algorithm: every reply carries the Coordinator's clock; every
# request carries the worker's latest estimate of its offset from it.
SERVER_TIME_HEADER = "X-Sentinel-Server-Time"
CLOCK_ESTIMATE_HEADER = "X-Sentinel-Clock-Estimate"
CLOCK_SAMPLES = 8
# Restore detection: every reply that opened the store names the database
# (its generation) and how far it has got (its commit counter), both after the
# request's own commit and before the request changed anything. The worker
# compares the second with what it saw earlier, so the request's own commit
# cannot hide a restore that lost exactly one commit.
STORE_GENERATION_HEADER = "X-Sentinel-Store-Generation"
STORE_COMMIT_HEADER = "X-Sentinel-Store-Commit"
STORE_COMMIT_BEFORE_HEADER = "X-Sentinel-Store-Commit-Before"
GENERATION = re.compile(r"[0-9a-f]{32}")
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
    "Result answers a superseded request": "superseded_request",
    "Worker clock skew exceeds the bound": "clock_skew",
    "Stale ownership epoch": "stale_epoch",
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


def inbound_trace(headers):
    """The caller's span from exactly one well-formed traceparent header, else None.

    Call only after authentication. The value labels log lines; it grants nothing.
    """
    values = headers.get_all("traceparent") or []
    return tracing.parse_traceparent(values[0]) if len(values) == 1 else None


def _message_ids(message):
    return ({"incident_id": message.get("incident_id"), "action_id": message.get("action_id")}
            if isinstance(message, dict) else {})


def _result_kind(body):
    result = body.get("result") if isinstance(body, dict) else None
    return result.get("kind") if isinstance(result, dict) and isinstance(result.get("kind"), str) else None


def clock_estimate(headers):
    """The worker's (offset, uncertainty) report from exactly one well-formed header, else None."""
    values = headers.get_all(CLOCK_ESTIMATE_HEADER) or []
    return parse_clock_estimate(values[0]) if len(values) == 1 else None


def store_position(store):
    """The store's (generation, commit counter) for the reply headers, or None if it cannot be read."""
    try:
        return store.position()
    except sqlite3.Error as error:
        log.failure(LOG, "http.store_position.failed", error)
        return None


def handler(database, services, lease_seconds=30, retention_seconds=86400,
            request_timeout=15, stopping=None, clock=None, dependency_requests=False, max_clock_skew=None):
    def server_time():
        """The Coordinator's clock: the store clock when one is injected, else the system clock."""
        return (clock() if clock is not None else datetime.now(UTC)).timestamp()

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
                except Exception as error:  # noqa: BLE001 - any failure to open the store means not ready
                    log.failure(LOG, "http.readiness.failed", error)
                    self.reply(503, {"ready": False})
                    return
            self.reply(200, {"ready": True})

        def do_POST(self):
            started, operation = time.monotonic(), OPERATIONS.get(self.path, "unknown")
            ids, outcome, reason, error_type, span = {}, None, None, None, None
            store, position, before = None, None, None
            with ExitStack() as scope:
                try:
                    if stopping is not None and stopping.is_set():
                        raise ProtocolError(503, "Coordinator is stopping; retry the saved request")
                    name, role = authenticate(self.headers, services)
                    ids["identity"] = name
                    estimate = clock_estimate(self.headers)  # Read only after authentication.
                    # Only an authenticated caller's traceparent is read, so a
                    # forged header in a refused request never reaches a log line.
                    parent = inbound_trace(self.headers)
                    if parent is not None:
                        span = scope.enter_context(tracing.start_span(
                            "coordinator." + operation, parent=parent, operation=operation, identity=name))
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
                    # Closed when this scope ends, after its position is read for the reply.
                    store = scope.enter_context(SQLiteStore(database, clock=clock))
                    # Read before this request changes anything: the worker compares it,
                    # not the counter after its own commit, with what it saw before.
                    before = store_position(store)
                    delivery = Delivery(store, lease_seconds=lease_seconds, retention_seconds=retention_seconds,
                                        dependency_requests=dependency_requests, max_clock_skew=max_clock_skew)
                    if self.path == "/v1/claim":
                        answer = delivery.claim(name, role, clock_estimate=estimate)
                        ids.update(delivery_ids(answer["delivery"]))
                        outcome = "claimed" if answer["delivery"] else "idle"
                    elif self.path == "/v1/clock":
                        answer = delivery.report_clock(name, estimate)
                        outcome = "idle"
                    elif self.path == "/v1/ingest":
                        answer = delivery.ingest(role, body["message"], identity=name)
                        outcome = "replay" if answer.get("replay") else "accepted"
                    elif self.path == "/v1/renew":
                        answer = delivery.renew(name, role, body["delivery_id"], body["lease_token"])
                        outcome = "renewed"
                    elif self.path == "/v1/dependencies":
                        answer = delivery.dependencies(role, body["proposal"])
                    elif self.path in ("/v1/result", "/v1/retry"):
                        answer = delivery.finish(name, role, body["delivery_id"], body["lease_token"],
                                                 body.get("result"), retry=self.path == "/v1/retry",
                                                 report=body.get("report"), clock_estimate=estimate)
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
                except Exception as exc:  # noqa: BLE001 - an internal error answers 503; access_log records error_type
                    status, error_type = 503, type(exc).__name__
                    response = {"error": "Coordinator unavailable; retry the saved request"}
                if store is not None:  # Read after this request's own commit, so it includes it.
                    position = store_position(store)
                if span is not None:
                    span.set(http_status=status, outcome=outcome, reason=reason)
                    if status >= 400:
                        span.fail(error_type)
                    elif outcome in {"idle", "renewed"}:
                        span.quiet()
                # Log before replying, so the line exists once the client sees the answer.
                access_log(LOG, operation, status, started, outcome=outcome, reason=reason,
                           error_type=error_type, **ids)
            if status == 200 and operation == "result":
                # Inert outside lab fault runs (runtime/fault_points.py): committed, not yet answered.
                fault_points.checkpoint("coordinator.after_commit_before_reply", qualifier=_result_kind(body))
            self.reply(status, response, position, before)

        def reply(self, status, value, position=None, before=None):
            data = json.dumps(value, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                if position is not None and position[0] is not None:
                    self.send_header(STORE_GENERATION_HEADER, position[0])
                    self.send_header(STORE_COMMIT_HEADER, str(position[1]))
                    if before is not None and before[0] == position[0]:
                        self.send_header(STORE_COMMIT_BEFORE_HEADER, str(before[1]))
                # Read as late as possible, so the worker's RTT brackets it.
                self.send_header(SERVER_TIME_HEADER, f"{server_time():.6f}")
                self.end_headers()
                self.wfile.write(data)
            except (ConnectionError, socket.timeout):
                # The transaction may already be committed. The worker must
                # replay its saved body to recover the durable receipt.
                self.close_connection = True
    return WorkerHandler


class ClockEstimator:
    """Offset of the Coordinator's clock from this worker's, by Cristian's algorithm.

    Each exchange gives one sample: offset = server_time - (t_send + rtt / 2),
    where t_send is this worker's wall clock when it sent the request and rtt is
    measured on the monotonic clock, so a wall-clock step cannot distort it.
    The true offset lies within rtt / 2 of the sample, assuming the reply was
    stamped somewhere in the round trip. The estimate is the sample with the
    smallest round trip among the last CLOCK_SAMPLES.

    While the wall clock runs steadily, every sample's interval
    [offset - uncertainty, offset + uncertainty] holds the true offset, so
    any two intervals overlap. A new sample whose interval misses the current
    estimate's interval shows the wall clock has stepped (an NTP step, a VM
    resume, a manual change): the older samples are dropped and the new one
    becomes the estimate at once, so a step is seen on the first exchange
    after it. A step smaller than the two uncertainties together can go
    unnoticed; the estimate is then still within its own uncertainty plus
    twice the new sample's of the true offset. within() also requires the
    newest sample to be inside the bound, since that sample alone brackets
    the offset as it is now.
    """
    def __init__(self, *, clock=time.time, monotonic=time.monotonic, samples=CLOCK_SAMPLES):
        self.clock, self.monotonic = clock, monotonic
        self.samples = deque(maxlen=samples)
        self._lock = threading.Lock()  # The lease renewal thread shares the client.

    def start(self):
        return self.clock(), self.monotonic()

    def observe(self, started, server_time):
        wall, mono = started
        rtt = max(0.0, self.monotonic() - mono)
        try:
            server = float(server_time)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(server):
            return None
        sample = (server - (wall + rtt / 2), rtt / 2)
        with self._lock:
            if self.samples:
                offset, uncertainty = min(self.samples, key=lambda kept: kept[1])
                if abs(sample[0] - offset) > sample[1] + uncertainty:
                    self.samples.clear()  # The intervals cannot both hold the true offset: a clock step.
            self.samples.append(sample)
        return sample

    def estimate(self):
        """(offset, uncertainty) in seconds, or None before the first exchange."""
        with self._lock:
            return min(self.samples, key=lambda sample: sample[1]) if self.samples else None

    def within(self, bound):
        with self._lock:
            newest = self.samples[-1] if self.samples else None
        return within_skew_bound(self.estimate(), bound) and within_skew_bound(newest, bound)


def parse_store_position(generation, commit):
    """(generation, commit counter) from the two reply headers, or None when absent or malformed."""
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        return None
    if not isinstance(commit, str) or not commit.isascii() or not commit.isdigit() or len(commit) > 18:
        return None
    return generation, int(commit)


class StoreWatch:
    """Which Coordinator database this worker talks to, and the furthest it has seen it get.

    The first reply's generation is adopted. A reply naming another
    generation means the database was recreated or replaced. A reply with the
    same generation and a commit counter lower than one already seen before
    the request was sent means the database went back in time: it was
    restored from an earlier copy. Either way change records the first such
    reply, and stays set until reset(); the worker then stops claiming
    (runtime/remote_worker.py::RemoteWorker.store_status).

    The counter compared is the one the Coordinator read before the request
    changed anything (before), when the reply carries it: the counter after
    the request's own commit would hide a restore that lost exactly one
    commit. A reply from a Coordinator that sends no before value is compared
    by its counter after the commit only. highest only ever grows.

    Only replies received before a request was sent are compared with its
    reply (floor()), since requests in flight together (a lease renewal
    beside a claim) may be answered in either order.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._replies = threading.local()  # The latest reply each thread received.
        self.generation = self.highest = self.change = None

    def seed(self, generation, commit, change=None):
        """Start from a remembered position, and a change already recorded, if any."""
        with self._lock:
            self.generation, self.highest, self.change = generation, commit, change

    def reset(self):
        """Forget everything; the next reply's database is adopted (after an operator accepted a restore)."""
        self.seed(None, None, None)

    def floor(self):
        with self._lock:
            return self.highest

    def last_seen(self):
        """(generation, counter after its commit) of the latest reply this thread received, or None.

        Per thread, so a lease renewal answered on another thread is never
        taken for the reply to this thread's own request.
        """
        return getattr(self._replies, "position", None)

    def observe(self, floor, generation, commit, before=None):
        """Record one reply's position; return the change it reveals, if it is the first.

        before is the reply's counter from before the request changed
        anything; it is ignored when absent or malformed.
        """
        position = parse_store_position(generation, commit)
        if position is None:
            return None
        generation, commit = position
        start = parse_store_position(generation, before)
        compared = min(commit, start[1]) if start is not None else commit
        self._replies.position = position
        with self._lock:
            if self.generation is None:
                self.generation, self.highest = generation, commit
                return None
            if generation != self.generation:
                reason = "generation_changed"
            elif floor is not None and compared < floor:
                reason = "rolled_back"
            else:
                if self.change is None:
                    self.highest = max(self.highest or 0, commit)
                return None
            if self.change is not None:
                return None
            self.change = {"reason": reason, "previous_generation": self.generation,
                           "previous_commit": self.highest, "generation": generation, "commit": compared}
            return self.change


class WorkerClient:
    """One bounded POST. Callers retain the immutable result for explicit retry.

    clock is this worker's wall clock (seconds), used only to estimate its
    offset from the Coordinator's clock (ClockEstimator). store watches the
    Coordinator database's generation and commit counter (StoreWatch); pass
    one StoreWatch to every client of one worker so they share it.
    """
    def __init__(self, url, identity, token, *, timeout=15, tls_context=None, clock=time.time, store=None):
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
        self.clock = ClockEstimator(clock=clock)
        self.store = store if store is not None else StoreWatch()

    def post(self, operation, body):
        if operation not in {"claim", "ingest", "result", "retry", "renew", "dependencies", "clock"}:
            raise ValueError("Unknown operation")
        encoded = json.dumps(body, allow_nan=False).encode()
        if not isinstance(body, dict) or not 0 < len(encoded) <= MAX_BODY:
            raise ValueError("Request must be a JSON object of at most 1 MiB")
        if self.url.scheme == "https":
            connection = HTTPSConnection(self.url.hostname, self.url.port, timeout=self.timeout, context=self.tls_context)
        else:
            connection = HTTPConnection(self.url.hostname, self.url.port, timeout=self.timeout)
        try:
            headers = {"Authorization": "Bearer " + self.token, "X-Service-Identity": self.identity,
                       "Content-Type": "application/json"}
            traceparent = tracing.current_traceparent()
            if traceparent is not None:
                headers["traceparent"] = traceparent
            estimate = self.clock.estimate()
            if estimate is not None:
                headers[CLOCK_ESTIMATE_HEADER] = f"{estimate[0]:.6f};{estimate[1]:.6f}"
            floor = self.store.floor()
            started = self.clock.start()
            connection.request("POST", "/v1/" + operation, encoded, headers)
            response = connection.getresponse()
            header = getattr(response, "getheader", None)  # Absent on minimal stand-in responses.
            self.clock.observe(started, header(SERVER_TIME_HEADER) if header else None)
            if header:
                self.store.observe(floor, header(STORE_GENERATION_HEADER), header(STORE_COMMIT_HEADER),
                                   header(STORE_COMMIT_BEFORE_HEADER))
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
    parser.add_argument("--max-clock-skew-seconds", type=float,
                        help="Refuse claims from a worker whose reported clock offset plus uncertainty exceeds this")
    args = parser.parse_args(argv)
    if bool(args.cert) != bool(args.key) or (not args.cert and not ipaddress.ip_address(args.bind).is_loopback):
        parser.error("Non-loopback serving requires --cert and --key")
    if (args.lease_seconds < 1 or args.retention_seconds < 1
            or not math.isfinite(args.request_timeout) or args.request_timeout <= 0):
        parser.error("Lease, retention and request timeout must be positive and finite")
    if args.max_clock_skew_seconds is not None and not 0 < args.max_clock_skew_seconds <= 3600:
        parser.error("The clock skew bound must be positive and at most one hour")
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
                        args.request_timeout, stopping, max_clock_skew=args.max_clock_skew_seconds), tls_context=tls,
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
