"""Instrumented caller/callee validation workload; not Online Boutique code."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import json
import os
import threading
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.metrics import Observation
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

resource = Resource.create({"service.name": os.environ["SERVICE_NAME"],
    "k8s.namespace.name": os.environ["POD_NAMESPACE"], "k8s.pod.uid": os.environ["POD_UID"]})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(), schedule_delay_millis=500))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("sentinel.observer.validation")
logs = LoggerProvider(resource=resource)
logs.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(), schedule_delay_millis=500))
logger = logging.getLogger("sentinel.validation")
logger.setLevel(logging.INFO)
logger.addHandler(LoggingHandler(logger_provider=logs))
lock = threading.Lock()
latest = {"sequence": 0, "failed": 0, "exported": 0}
fault = {"enabled": os.environ.get("FAIL_REQUESTS") == "true"}


def current_result(_options):
    # Export only when an actual application request produced a new reading.
    with lock:
        if latest["sequence"] == latest["exported"]:
            return []
        latest["exported"] = latest["sequence"]
        return [Observation(latest["failed"])]


metrics = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
    OTLPMetricExporter(), export_interval_millis=1000)])
meter = metrics.get_meter("sentinel.observer.validation")
meter.create_observable_gauge("sentinel.request.failed", callbacks=[current_result], unit="1")
duration = meter.create_histogram("sentinel.request.duration", unit="s")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # Operator-only test control in this validation workload. It changes
        # real request behavior without introducing a Kubernetes rollout fault.
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.path != "/validation/fault" or not 0 < length <= 64:
                raise ValueError("Invalid validation control")
            value = json.loads(self.rfile.read(length))
            if set(value) != {"enabled"} or type(value["enabled"]) is not bool:
                raise ValueError("Expected enabled boolean")
            with lock:
                fault["enabled"] = value["enabled"]
            self.send_response(200)
        except (ValueError, TypeError):
            self.send_response(400)
        self.end_headers()

    def do_GET(self):
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ready")
            return
        started = time.monotonic()
        carrier = {key.lower(): value for key, value in self.headers.items()}
        with tracer.start_as_current_span("request", context=extract(carrier), kind=trace.SpanKind.SERVER) as span:
            with lock:
                status = 503 if fault["enabled"] else 200
            upstream = os.environ.get("UPSTREAM_URL")
            if upstream:
                with tracer.start_as_current_span("dependency.request", kind=trace.SpanKind.CLIENT) as client:
                    client.set_attribute("peer.service", "otel-callee")
                    headers = {}
                    inject(headers)
                    try:
                        with urlopen(Request(upstream, headers=headers), timeout=3) as response:
                            status = response.status
                    except URLError:
                        status = 503
                        client.set_status(trace.StatusCode.ERROR)
            span.set_attribute("http.response.status_code", status)
            if status != 200:
                span.set_status(trace.StatusCode.ERROR)
                logger.error("Application request failed", extra={"event.name": "request.failed"})
            else:
                logger.info("Application request succeeded", extra={"event.name": "request.succeeded"})
            with lock:
                latest.update(sequence=latest["sequence"] + 1, failed=int(status != 200))
            duration.record(time.monotonic() - started)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b"request complete")

    def log_message(self, *_):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
