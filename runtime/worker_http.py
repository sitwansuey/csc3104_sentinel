"""Run with python -m runtime.worker_http --help. Secrets are never logged."""
import argparse
import hmac
import ipaddress
import json
import os
import socket
import ssl
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from runtime.worker_transport import Delivery, INGRESS, ProtocolError, TOPICS
from services.coordinator import SQLiteStore

MAX_BODY = 1024 * 1024


def identities(path):
    entries = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    configured = {}
    for name, entry in entries.items():
        role = entry["role"]
        token = os.environ[entry["token_env"]]
        if (role not in set(TOPICS) | set(INGRESS) or len(token) < 32 or not token.isascii()
                or any(token == secret for _, secret in configured.values())):
            raise ValueError("Invalid role, weak credential or credential reused between services")
        configured[name] = (role, token)
    return configured


def handler(database, services, lease_seconds=30):
    class WorkerHandler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(15)

        def do_POST(self):
            try:
                name = self.headers.get("X-Service-Identity", "")
                auth = self.headers.get("Authorization", "")
                if name not in services or not hmac.compare_digest(
                        auth.encode(), ("Bearer " + services[name][1]).encode()):
                    raise ProtocolError(401, "Invalid credentials")
                role = services[name][0]
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
                with SQLiteStore(database) as store:
                    delivery = Delivery(store, lease_seconds=lease_seconds)
                    if self.path == "/v1/claim":
                        answer = delivery.claim(name, role)
                    elif self.path == "/v1/ingest":
                        answer = delivery.ingest(role, body["message"])
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
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    return WorkerHandler


class WorkerClient:
    """One bounded POST. Callers retain the immutable result for explicit retry."""
    def __init__(self, url, identity, token, *, timeout=15, tls_context=None):
        self.url = urlsplit(url)
        if self.url.scheme != "https" and not (self.url.scheme == "http" and
                self.url.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError("Use HTTPS outside loopback")
        self.identity, self.token, self.timeout = identity, token, timeout
        self.tls_context = tls_context

    def post(self, operation, body):
        if operation not in {"claim", "ingest", "result", "retry"}:
            raise ValueError("Unknown operation")
        if self.url.scheme == "https":
            connection = HTTPSConnection(self.url.hostname, self.url.port, timeout=self.timeout, context=self.tls_context)
        else:
            connection = HTTPConnection(self.url.hostname, self.url.port, timeout=self.timeout)
        try:
            connection.request("POST", "/v1/" + operation, json.dumps(body).encode(),
                {"Authorization": "Bearer " + self.token, "X-Service-Identity": self.identity,
                 "Content-Type": "application/json"})
            response = connection.getresponse()
            value = json.loads(response.read(MAX_BODY + 1024))
            if response.status != 200:
                raise ProtocolError(response.status, value.get("error", "Request failed"))
            return value
        finally:
            connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--identities", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--lease-seconds", type=int, default=30)
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key) or (not args.cert and not ipaddress.ip_address(args.bind).is_loopback):
        parser.error("Non-loopback serving requires --cert and --key")
    if args.lease_seconds < 1:
        parser.error("--lease-seconds must be positive")
    entries = identities(args.identities)
    with SQLiteStore(args.db) as store:
        Delivery(store, lease_seconds=args.lease_seconds)
    with ThreadingHTTPServer((args.bind, args.port), handler(args.db, entries, args.lease_seconds)) as server:
        if args.cert:
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(args.cert, args.key)
            server.socket = tls.wrap_socket(server.socket, server_side=True)
        print(f"Coordinator worker API listening on {args.bind}:{args.port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
