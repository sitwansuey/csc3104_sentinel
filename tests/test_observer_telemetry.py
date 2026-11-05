from copy import deepcopy
from contextlib import closing
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import gzip
import io
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.otlp_receiver import MAX_BYTES, make_receiver
from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore
from runtime.observer_fleet import load_targets, state_path, run_fleet
from runtime.observer_pipeline import status_file
from services.observer import cli as observer_cli
from services.observer.core import Observer
from services.observer.telemetry import TelemetryError, TelemetryStore, normalize

NOW = datetime(2026, 10, 26, 0, 0, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]


def ns(at):
    return str(int(at.timestamp()) * 1_000_000_000 + at.microsecond * 1000)


def attrs(values):
    return [{"key": k, "value": {"stringValue": v}} for k, v in values.items()]


def payload(signal, at=NOW, *, pod="pod-1", service="demo-api", namespace="sentinel-demo", value=1):
    resource = {"attributes": attrs({"k8s.namespace.name": namespace, "k8s.pod.uid": pod, "service.name": service})}
    if signal == "traces":
        record = {"traceId": "a" * 32, "spanId": "b" * 16,
            "startTimeUnixNano": ns(at - timedelta(milliseconds=20)), "endTimeUnixNano": ns(at),
            "kind": 2, "status": {"code": 2},
            "attributes": attrs({"rpc.method": "GetQuote", "authorization": "private-token"}),
            "name": "private/path?token=secret", "events": [{"name": "private"}]}
        return {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [record]}]}]}
    if signal == "logs":
        record = {"timeUnixNano": ns(at), "severityNumber": 17, "traceId": "a" * 32,
                  "spanId": "b" * 16, "body": {"stringValue": "password=private"}}
        return {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [record]}]}]}
    record = {"name": "request.failed", "unit": "1", "gauge": {"dataPoints": [
        {"timeUnixNano": ns(at), "asDouble": value}]}}
    return {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"metrics": [record]}]}]}


def observation(at=NOW):
    item = json.loads((ROOT / "data/sentinel/samples/observations.json").read_text())["healthy"][0]
    item["captured_at"] = at.isoformat()
    item["deployment"]["metadata"]["uid"] = "deployment-1"
    item["pods"][0]["metadata"]["uid"] = "pod-1"
    return item


RULES = [{"name": "request.failed", "unit": "1", "above": 0}]
POLICY = {"detection": {"failure_duration_seconds": 10, "max_sample_gap_seconds": 20}}


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.store = TelemetryStore(self.path / "telemetry.sqlite")

    def attach(self, at, **kwargs):
        item = observation(at)
        item["telemetry"] = self.store.context(item, ["demo-api"], thresholds=RULES, **kwargs)
        return item

    def test_real_otlp_shapes_are_projected_and_sensitive_fields_omitted(self):
        for signal in ("traces", "metrics", "logs"):
            self.assertEqual(self.store.ingest(signal, payload(signal), now=NOW), {"accepted": 1, "rejected": 0})
        context = self.store.context(observation(), ["demo-api"])
        encoded = json.dumps(context)
        self.assertNotIn("private", encoded)
        self.assertNotIn("secret", encoded)
        self.assertEqual(context["records"]["traces"][0]["trace_id"], context["records"]["logs"][0]["trace_id"])

    def test_retries_and_reopening_do_not_duplicate_evidence(self):
        for _ in range(2):
            TelemetryStore(self.store.path).ingest("traces", payload("traces"), now=NOW)
        self.assertEqual(len(self.store.context(observation(), ["demo-api"])["records"]["traces"]), 1)

    def test_changed_span_or_metric_identity_is_rejected_atomically(self):
        for signal in ("traces", "metrics"):
            self.store.ingest(signal, payload(signal), now=NOW)
            changed = payload(signal)
            if signal == "traces":
                changed["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"]["code"] = 1
            else:
                changed["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"][0]["asDouble"] = 99
            with self.assertRaises(TelemetryError):
                self.store.ingest(signal, changed, now=NOW)

    def test_unknown_old_recreated_or_other_namespace_evidence_cannot_join(self):
        for options in ({"pod": "old-pod"}, {"namespace": "other"}, {"service": "other"}):
            self.store.ingest("metrics", payload("metrics", **options), now=NOW)
        context = self.attach(NOW)["telemetry"]
        self.assertEqual(context["records"]["metrics"], [])
        self.assertFalse(context["thresholds_complete"])
        self.assertEqual(context["coverage"]["metrics"]["status"], "unavailable")

    def test_missing_identity_future_and_old_records_report_rejection(self):
        for options in ({"pod": ""}, {"at": NOW + timedelta(seconds=1)}, {"at": NOW - timedelta(hours=1)}):
            self.assertEqual(self.store.ingest("metrics", payload("metrics", **options), now=NOW)["rejected"], 1)

    def test_unrepresentable_numeric_point_reports_rejection(self):
        result = self.store.ingest("metrics", payload("metrics", value=10 ** 400), now=NOW)
        self.assertEqual(result, {"accepted": 0, "rejected": 1})

    def test_metric_threshold_can_detect_failure_with_ready_pods_and_survives_restart(self):
        start = NOW - timedelta(seconds=10)
        self.store.ingest("metrics", payload("metrics", start), now=start)
        observer = Observer(POLICY)
        self.assertIsNone(observer.observe(self.attach(start)))
        self.store.ingest("metrics", payload("metrics", NOW), now=NOW)
        observer = Observer.from_state(POLICY, observer.export_state())
        incident = observer.observe(self.attach(NOW))
        self.assertIsNotNone(incident)
        self.assertIn("otel_metric_alert", {s["source"] for s in incident["signals"]})
        self.assertIsNone(Observer.from_state(POLICY, observer.export_state()).observe(self.attach(NOW + timedelta(seconds=1))))

    def test_repolling_the_same_metric_does_not_extend_failure_window(self):
        self.store.ingest("metrics", payload("metrics"), now=NOW)
        observer = Observer(POLICY)
        observer.observe(self.attach(NOW))
        self.assertIsNone(observer.observe(self.attach(NOW + timedelta(seconds=15))))
        self.assertIsNone(Observer.from_state(POLICY, observer.export_state()).observe(self.attach(NOW + timedelta(seconds=16))))

    def test_missing_metrics_after_emission_do_not_fake_recovery_or_duplicate(self):
        observer = Observer({"detection": {"failure_duration_seconds": 0}})
        self.store.ingest("metrics", payload("metrics"), now=NOW)
        self.assertIsNotNone(observer.observe(self.attach(NOW)))
        absent_at = NOW + timedelta(seconds=61)
        self.assertIsNone(observer.observe(self.attach(absent_at)))
        observer = Observer.from_state({"detection": {"failure_duration_seconds": 0}}, observer.export_state())
        fresh_at = absent_at + timedelta(seconds=1)
        self.store.ingest("metrics", payload("metrics", fresh_at), now=fresh_at)
        self.assertIsNone(observer.observe(self.attach(fresh_at)))

    def test_missing_metric_point_for_one_owned_pod_is_unknown(self):
        self.store.ingest("metrics", payload("metrics"), now=NOW)
        item = observation()
        other = deepcopy(item["pods"][0])
        other["metadata"]["uid"] = "pod-2"
        item["pods"].append(other)
        context = self.store.context(item, ["demo-api"], thresholds=RULES)
        self.assertFalse(context["thresholds_complete"])
        self.assertEqual(context["alerts"], [])

    def test_new_generation_with_missing_metrics_remains_checkpointable(self):
        policy = {"detection": {"failure_duration_seconds": 0}}
        observer = Observer(policy)
        self.store.ingest("metrics", payload("metrics"), now=NOW)
        self.assertIsNotNone(observer.observe(self.attach(NOW)))
        item = self.attach(NOW + timedelta(seconds=61))
        item["deployment"]["metadata"]["generation"] += 1
        item["deployment"]["status"]["observedGeneration"] = item["deployment"]["metadata"]["generation"]
        self.assertIsNone(observer.observe(item))
        restored = Observer.from_state(policy, observer.export_state())
        self.assertEqual(restored.export_state()["episodes"], [])

    def test_display_truncation_does_not_hide_latest_threshold_point(self):
        # Context displays only 20 records, while thresholds read up to 1,000.
        for seconds in range(30):
            at = NOW - timedelta(seconds=seconds)
            self.store.ingest("metrics", payload("metrics", at, value=int(seconds == 0)), now=NOW)
        context = self.attach(NOW)["telemetry"]
        self.assertTrue(context["coverage"]["metrics"]["truncated"])
        self.assertTrue(context["thresholds_complete"])
        self.assertEqual(len(context["alerts"]), 1)

    def test_threshold_budget_overflow_is_unknown(self):
        document = payload("metrics")
        points = document["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]["gauge"]["dataPoints"]
        points[:] = [{"timeUnixNano": ns(NOW - timedelta(milliseconds=n)), "asDouble": 1} for n in range(1000)]
        self.store.ingest("metrics", document, now=NOW)
        self.store.ingest("metrics", payload("metrics", NOW - timedelta(milliseconds=1001)), now=NOW)
        context = self.attach(NOW)["telemetry"]
        self.assertFalse(context["thresholds_complete"])
        self.assertEqual(context["alerts"], [])

    def test_context_and_incident_are_atomic_and_delivery_replays_identically(self):
        self.store.ingest("metrics", payload("metrics"), now=NOW)
        item = self.attach(NOW)
        target = {"cluster_id": item["cluster_id"], "namespace": item["deployment"]["metadata"]["namespace"],
                  "kind": "Deployment", "name": item["deployment"]["metadata"]["name"]}
        with ObserverStore(self.path / "worker.sqlite", {"detection": {"failure_duration_seconds": 0}}, target) as worker:
            incident = worker.observe(item)
            self.assertEqual(worker.incident_context(incident["incident_id"]), item["telemetry"])
            self.assertEqual(worker.flush(lambda _: (_ for _ in ()).throw(OSError()))["error"], "OSError")
            sink = LocalCoordinatorSink(self.path / "coordinator.sqlite")
            sink(incident)
            self.assertEqual(worker.flush(sink)["delivered"], 1)
        with closing(sqlite3.connect(self.path / "coordinator.sqlite")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)

    def test_fleet_scope_and_state_files_are_explicit_and_distinct(self):
        target = {"namespace": "demo", "deployment": "first"}
        path = self.path / "targets.json"
        path.write_text(json.dumps([target, {**target, "deployment": "second"}]))
        first, second = load_targets(path)
        self.assertNotEqual(state_path("worker.sqlite", "lab", first), state_path("worker.sqlite", "lab", second))
        path.write_text(json.dumps([target, target]))
        with self.assertRaises(ValueError):
            load_targets(path)

    def test_failed_fleet_target_does_not_starve_other_workload(self):
        targets = [{"namespace": "sentinel-demo", "deployment": name} for name in ("first", "second")]
        config = self.path / "targets.json"
        config.write_text(json.dumps(targets))
        args = SimpleNamespace(targets=config, telemetry_db=None, state=self.path / "fleet.sqlite",
            cluster_id="lab", collect_logs=False, poll_count=1, interval_seconds=1)

        class Collector:
            def __init__(self, reader, *, namespace, name, cluster_id, collect_logs):
                self.name = name
                self.target = {"cluster_id": cluster_id, "namespace": namespace, "kind": "Deployment", "name": name}

            def collect(self):
                if self.name == "first":
                    raise OSError("read unavailable")
                item = observation()
                item["cluster_id"] = "lab"
                item["deployment"]["metadata"]["name"] = self.name
                return item

        with patch("runtime.observer_fleet.KubernetesEvidenceCollector", Collector), redirect_stdout(io.StringIO()):
            self.assertEqual(run_fleet(args, None, POLICY, lambda _: None, threading.Event()), 1)
        first, second = load_targets(config)
        self.assertEqual(status_file(state_path(args.state, "lab", first))["collection_errors"], 1)
        self.assertEqual(status_file(state_path(args.state, "lab", second))["polls"], 1)

    def test_cli_rejects_thresholds_without_telemetry_source(self):
        policy = self.path / "policy.json"
        policy.write_text(json.dumps({**POLICY, "telemetry": {"metric_thresholds": RULES}}))
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            observer_cli.main(["run", "--context=lab", "--cluster-id=lab", "--namespace=demo",
                               "--deployment=api", "--policy=" + str(policy)])
        self.assertEqual(error.exception.code, 2)


class ReceiverTests(unittest.TestCase):
    def test_http_auth_json_gzip_partial_success_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            server = make_receiver(TelemetryStore(Path(directory) / "t.sqlite"), port=0, token="test-token")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/v1/metrics"
                document = payload("metrics", datetime.now(timezone.utc))
                raw = json.dumps(document).encode()
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url, raw, {"Content-Type": "application/json"}))
                with closing(error.exception) as response:
                    self.assertEqual(response.code, 401)
                request = Request(url, gzip.compress(raw), {"Content-Type": "application/json", "Content-Encoding": "gzip", "Authorization": "Bearer test-token"})
                with urlopen(request) as response:
                    self.assertEqual(json.load(response), {})
                invalid = payload("metrics", datetime.now(timezone.utc), pod="")
                with urlopen(Request(url, json.dumps(invalid).encode(), {"Content-Type": "application/json", "Authorization": "Bearer test-token"})) as response:
                    self.assertEqual(json.load(response)["partialSuccess"]["rejectedDataPoints"], "1")
                cases = [
                    (raw, {"Content-Type": "application/x-protobuf"}, 415),
                    (b"invalid-gzip", {"Content-Type": "application/json", "Content-Encoding": "gzip"}, 400),
                    (b"{}", {"Content-Type": "application/json", "Content-Length": str(MAX_BYTES + 1)}, 413),
                    (gzip.compress(b" " * (MAX_BYTES + 1)), {"Content-Type": "application/json", "Content-Encoding": "gzip"}, 413),
                    (raw, {"Content-Type": "application/json", "Authorization": "Bearer t\u00e9st"}, 401),
                ]
                for data, headers, status in cases:
                    headers = {"Authorization": "Bearer test-token", **headers}
                    with self.subTest(status=status), self.assertRaises(HTTPError) as rejected:
                        urlopen(Request(url, data, headers))
                    with closing(rejected.exception) as response:
                        self.assertEqual(response.code, status)
                with self.assertRaises(ValueError):
                    make_receiver(None, bind="0.0.0.0")
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
