"""Disposable SDK-instrumented workload and Collector file-export bridge."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

ROLE = os.environ.get("ROLE", "checkout")
FAULT = Path("/tmp/unhealthy")
if ROLE != "bridge":
    provider = TracerProvider(resource=Resource.create({"service.name": ROLE,
        "k8s.namespace.name": os.environ["NAMESPACE"]}))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(
        endpoint="http://collector:4318/v1/traces", timeout=3)))
    trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sentinel-verifier-lab")
propagator = TraceContextTextMapPropagator()


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Fault injection is confined to this disposable test workload.
        if ROLE == "bridge" or self.path not in {"/lab/fail", "/lab/restore"}:
            self.reply(404, {})
            return
        if self.path == "/lab/fail":
            FAULT.touch()
        else:
            FAULT.unlink(missing_ok=True)
        self.reply(200, {"fault": FAULT.exists()})

    def do_GET(self):
        if ROLE == "bridge":
            try:
                path = Path("/evidence/traces.json")
                if path.stat().st_size > 2_000_000:
                    raise ValueError("Export too large")
                resources = []
                for line in path.read_text().splitlines():
                    if line.strip():
                        resources.extend(json.loads(line)["resourceSpans"])
                self.reply(200, {"resourceSpans": resources})
            except (OSError, ValueError, KeyError):
                self.reply(503, {"error": "Export unavailable"})
            return
        if self.path in {"/healthz", "/readyz"}:
            self.reply(503 if FAULT.exists() else 200, {"service": ROLE})
            return
        if self.path != "/request":
            self.reply(404, {})
            return
        context = propagator.extract({key.lower(): value for key, value in self.headers.items()})
        with tracer.start_as_current_span("request", context=context, kind=SpanKind.SERVER):
            status = 503 if FAULT.exists() else 200
            if ROLE == "checkout":
                with tracer.start_as_current_span("payment", kind=SpanKind.CLIENT):
                    headers = {}
                    propagator.inject(headers)
                    try:
                        with urlopen(Request("http://payment:8080/request", headers=headers), timeout=3) as response:
                            status = response.status
                    except HTTPError as error:
                        status = error.code
                        error.close()
                    except OSError:
                        status = 503
            self.reply(status, {"service": ROLE})


if __name__ == "__main__":
    with ThreadingHTTPServer(("0.0.0.0", 8080), Handler) as server:
        server.serve_forever()
