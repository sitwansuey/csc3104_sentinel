"""Envoy HTTP ext_authz endpoint; private loopback sidecar, no cluster credentials."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from runtime.security_store import record
from services.security.grpc_inspection import MAX_BODY, inspect_request


def make_server(journal, *, port=9001, stdout_events=False):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SentinelInspector"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_GET(self):
            self.send_error(405)

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
                result = inspect_request(self.path, "application/grpc",
                                         self.headers.get("grpc-encoding", ""), body,
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
            except Exception:
                self.send_error(503)
                return
            self.send_response(200 if result["verdict"] == "ALLOW" else 403)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

        def log_message(self, *args):
            pass

    # Share the proxy's network namespace. Never expose this port to clients.
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--stdout-events", action="store_true", help="Emit the same metadata-only audit records for log collection")
    args = parser.parse_args()
    with make_server(args.journal, port=args.port, stdout_events=args.stdout_events) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
