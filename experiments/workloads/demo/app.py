"""A dependency-free workload for Sentinel's first faulty-rollout experiment."""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in {"/", "/healthz", "/readyz"}:
            self.send_error(404)
            return
        payload = json.dumps({"service": "demo-api", "status": "ok"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main():
    if os.environ.get("STARTUP_FAILURE", "false").lower() == "true":
        print(json.dumps({
            "event": "startup_failed",
            "reason": "Invalid configuration: STARTUP_FAILURE=true",
        }), flush=True)
        return 1

    port = int(os.environ.get("PORT", "8080"))
    print(json.dumps({"event": "started", "port": port}), flush=True)
    with ThreadingHTTPServer(("0.0.0.0", port), Handler) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
