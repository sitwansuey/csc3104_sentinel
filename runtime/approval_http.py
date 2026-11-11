"""Authenticated approval boundary. Run python -m runtime.approval_http --help."""
import argparse
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler
import ipaddress
import json
import math
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit

from runtime.service_identity import ProtocolError, authenticate, load_identities
from runtime.worker_http import WorkerHTTPServer
from services.coordinator.approvals import ApprovalAccess, OPERATIONS, ROLES, load_permissions
from services.coordinator.store import CoordinatorError, SQLiteStore, _digest

MAX_BODY = 1024 * 1024


def handler(database, services, permissions, *, clock=None, max_lifetime_seconds=300,
            stopping=None, request_timeout=15):
    class ApprovalHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(request_timeout)

        def log_message(self, *_args):
            pass  # Credentials and approval bodies are never access-log inputs.

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
                        store.db.execute("SELECT 1 FROM approvals LIMIT 1").fetchone()
                except Exception:
                    self.reply(503, {"ready": False})
                    return
            self.reply(200, {"ready": True})

        def do_POST(self):
            try:
                if stopping is not None and stopping.is_set():
                    raise ProtocolError(503, "Approval authority is stopping")
                identity, role = authenticate(self.headers, services)
                lengths = self.headers.get_all("Content-Length", [])
                if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                    raise ProtocolError(400, "Ambiguous request framing")
                length = int(lengths[0])
                if not 0 < length <= MAX_BODY:
                    raise ProtocolError(413, "Invalid body length")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    raise ProtocolError(400, "Incomplete request body")
                prefix = "/v1/approvals/"
                operation = self.path[len(prefix):] if self.path.startswith(prefix) else ""
                with SQLiteStore(database, clock=clock) as store:
                    answer = ApprovalAccess(store, permissions, max_lifetime_seconds=max_lifetime_seconds).call(
                        identity, role, operation, json.loads(raw))
                self.reply(200, answer)
            except ProtocolError as exc:
                self.reply(exc.status, {"error": str(exc)})
            except CoordinatorError:
                self.reply(409, {"error": "Approval conflicts with current lifecycle or binding"})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid approval input"})
            except (TimeoutError, socket.timeout):
                self.reply(408, {"error": "Request timed out"})
            except Exception:
                self.reply(503, {"error": "Approval authority unavailable"})

        def reply(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (ConnectionError, socket.timeout):
                self.close_connection = True  # Exact retry can recover a committed record.
    return ApprovalHandler


class ApprovalClient:
    """Authenticated lookup callable for GuardAuthorizer/LiveGuardAuthorizer.

    Each call uses the authority, never an envelope or cached approval. Network
    failures propagate, so final execution authorization fails closed.
    """
    def __init__(self, url, identity, token, *, timeout=15, tls_context=None):
        self.url = urlsplit(url)
        if (self.url.scheme not in {"http", "https"} or not self.url.hostname
                or self.url.username or self.url.password or self.url.path not in {"", "/"}
                or self.url.query or self.url.fragment
                or self.url.scheme == "http" and self.url.hostname not in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Use an HTTPS authority URL outside loopback")
        self.url.port
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Timeout must be positive and finite")
        self.identity, self.token, self.timeout = identity, token, timeout
        self.tls_context = tls_context

    def post(self, operation, body):
        if operation not in OPERATIONS:
            raise ValueError("Unknown approval operation")
        encoded = json.dumps(body, allow_nan=False).encode()
        if not isinstance(body, dict) or not 0 < len(encoded) <= MAX_BODY:
            raise ValueError("Request must be a JSON object of at most 1 MiB")
        if self.url.scheme == "https":
            connection = HTTPSConnection(self.url.hostname, self.url.port, timeout=self.timeout, context=self.tls_context)
        else:
            connection = HTTPConnection(self.url.hostname, self.url.port, timeout=self.timeout)
        try:
            connection.request("POST", "/v1/approvals/" + operation, encoded,
                {"Authorization": "Bearer " + self.token, "X-Service-Identity": self.identity,
                 "Content-Type": "application/json"})
            response = connection.getresponse()
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                raise ProtocolError(502, "Approval response exceeds limit")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError) as exc:
                raise ProtocolError(502, "Approval authority returned invalid JSON") from exc
            if not isinstance(value, dict):
                raise ProtocolError(502, "Approval authority returned a non-object response")
            if response.status != 200:
                raise ProtocolError(response.status, value.get("error", "Approval request failed"))
            return value
        finally:
            connection.close()

    def __call__(self, proposal):
        return self.post("read", {"action_id": proposal["action_id"], "proposal_sha256": _digest(proposal)})["approval"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--identities", required=True)
    parser.add_argument("--permissions", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--max-lifetime-seconds", type=int, default=300)
    args = parser.parse_args()
    if (bool(args.cert) != bool(args.key)
            or not args.cert and not ipaddress.ip_address(args.bind).is_loopback):
        parser.error("Non-loopback serving requires --cert and --key")
    if not 1 <= args.max_lifetime_seconds <= 86400:
        parser.error("Approval lifetime must be between 1 and 86400 seconds")
    services = load_identities(args.identities, ROLES)
    permissions = load_permissions(args.permissions, services)
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    with SQLiteStore(args.db):
        pass
    tls = None
    if args.cert:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_cert_chain(args.cert, args.key)
    with WorkerHTTPServer((args.bind, args.port), handler(
            args.db, services, permissions, max_lifetime_seconds=args.max_lifetime_seconds),
            tls_context=tls) as server:
        print(f"Coordinator approval API listening on {args.bind}:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
