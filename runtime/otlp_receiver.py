"""Private bounded OTLP/HTTP JSON endpoint for the Observer's evidence store."""

import argparse
import gzip
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import sqlite3
import zlib

from services.observer_telemetry import TelemetryError, TelemetryStore

MAX_BYTES = 1_000_000


def make_receiver(store, *, bind="127.0.0.1", port=4319, token=None):
    if bind not in {"127.0.0.1", "0.0.0.0"} or (bind != "127.0.0.1" and not token):
        raise ValueError("Non-loopback OTLP binding requires a token")

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.reply(200 if self.path == "/healthz" else 404, {})

        def do_POST(self):
            self.connection.settimeout(10)
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                self.reply(401, {"message": "Unauthorized"})
                return
            signal = self.path.removeprefix("/v1/")
            if self.path not in {"/v1/traces", "/v1/metrics", "/v1/logs"}:
                self.reply(404, {})
                return
            if self.headers.get_content_type() != "application/json":
                self.reply(415, {"message": "OTLP JSON is required"})
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if not 0 <= length <= MAX_BYTES or self.headers.get("Transfer-Encoding"):
                    self.reply(413, {"message": "Invalid or oversized request"})
                    return
                body = self.rfile.read(length)
                encoding = self.headers.get("Content-Encoding", "identity")
                if encoding == "gzip":
                    with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                        body = compressed.read(MAX_BYTES + 1)
                elif encoding != "identity":
                    self.reply(415, {})
                    return
                if len(body) > MAX_BYTES:
                    self.reply(413, {})
                    return
                document = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON")))
                result = store.ingest(signal, document)
                key = {"traces": "rejectedSpans", "metrics": "rejectedDataPoints", "logs": "rejectedLogRecords"}[signal]
                self.reply(200, {"partialSuccess": {key: str(result["rejected"]),
                    "errorMessage": "Some records lacked valid bounded workload evidence"}} if result["rejected"] else {})
            except sqlite3.Error:
                self.reply(503, {"message": "Evidence store temporarily unavailable"})
            except (ValueError, TypeError, AttributeError, EOFError, OSError, RecursionError, zlib.error):
                self.reply(400, {"message": "Invalid OTLP evidence"})

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer((bind, port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4319)
    parser.add_argument("--token-file", type=Path)
    args = parser.parse_args(argv)
    token = args.token_file.read_text().strip() if args.token_file else None
    server = make_receiver(TelemetryStore(args.db), bind=args.bind, port=args.port, token=token)
    print(f"Observer OTLP JSON receiver listening on {args.bind}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
