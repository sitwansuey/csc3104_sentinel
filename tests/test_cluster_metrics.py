from copy import deepcopy
from contextlib import closing
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal
import json
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from runtime.cluster_metrics import LIST_LIMITS, SOURCE_NAMES, collect_cluster_snapshot, parse_quantity
from runtime.dashboard import CLUSTER_HISTORY_LIMIT, snapshot
from runtime.telemetry import emit_cluster_snapshot, emit_event
from scripts.observe_dashboard import main as collector_main


NOW = datetime(2026, 10, 25, 6, 0, 0, tzinfo=timezone.utc)


def resources():
    return {
        "nodes": [{"metadata": {"name": "node-a", "annotations": {"token": "DO-NOT-PUBLISH"}},
                   "status": {"conditions": [{"type": "Ready", "status": "True", "message": "DO-NOT-PUBLISH"},
                                               {"type": "MemoryPressure", "status": "False"}],
                              "capacity": {"cpu": "4", "memory": "8Gi"},
                              "allocatable": {"cpu": "3900m", "memory": "7Gi"}}}],
        "pods": [{"metadata": {"name": "frontend-a", "namespace": "boutique"},
                  "spec": {"nodeName": "node-a", "containers": [{"name": "server", "env": [{"name": "PASSWORD", "value": "DO-NOT-PUBLISH"}]}]},
                  "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}],
                             "containerStatuses": [{"name": "server", "ready": True, "restartCount": 2,
                                                     "state": {"running": {"startedAt": "2026-10-25T05:00:00Z"}},
                                                     "lastState": {"terminated": {"reason": "OOMKilled", "message": "DO-NOT-PUBLISH"}}}]}}],
        "events": [{"metadata": {"name": "frontend-event", "namespace": "boutique"}, "type": "Warning",
                    "reason": "BackOff", "involvedObject": {"kind": "Pod", "name": "frontend-a"},
                    "count": 3, "lastTimestamp": "2026-10-25T05:59:00Z", "message": "credential=DO-NOT-PUBLISH"}],
        "node_metrics": [{"metadata": {"name": "node-a"}, "usage": {"cpu": "250000000n", "memory": "1024Mi"},
                          "timestamp": "2026-10-25T05:59:48Z", "window": "30.125s"}],
        "pod_metrics": [{"metadata": {"name": "frontend-a", "namespace": "boutique"},
                         "timestamp": "2026-10-25T05:59:45Z", "window": "1m0s",
                         "containers": [{"name": "server", "usage": {"cpu": "125m", "memory": "64Mi"}}]}],
    }


def fake_runner(documents, failures=None):
    calls = []
    failures = failures or {}

    def run(command, **kwargs):
        calls.append((command, kwargs))
        if "--raw" in command:
            key = "node_metrics" if command[-1].endswith("/nodes") else "pod_metrics"
        else:
            key = command[5]
        if key in failures:
            failure = failures[key]
            if isinstance(failure, Exception):
                raise failure
            return subprocess.CompletedProcess(command, 1, "", failure)
        return subprocess.CompletedProcess(command, 0, json.dumps({"items": documents[key]}), "")

    return run, calls


def collect(documents=None, failures=None):
    runner, calls = fake_runner(documents or resources(), failures)
    result = collect_cluster_snapshot(context="kind-boutique", cluster_id="sentinel-lab",
                                      namespace="boutique", runner=runner, clock=lambda: NOW)
    return result, calls


class ClusterMetricsTests(unittest.TestCase):
    def test_units_are_exact_and_invalid_quantities_never_become_zero(self):
        self.assertEqual(parse_quantity("125000000n"), Decimal("0.125"))
        self.assertEqual(parse_quantity("3900m"), Decimal("3.900"))
        self.assertEqual(parse_quantity("1.5Gi", memory=True), Decimal(1610612736))
        self.assertEqual(parse_quantity("129e6", memory=True), Decimal(129000000))
        for quantity in ("-1", "NaN", "Infinity", "2Mib", "3foo", "1e9999999", True, 5, ""):
            with self.subTest(quantity=quantity), self.assertRaises(ValueError):
                parse_quantity(quantity)
        with self.assertRaises(ValueError):
            parse_quantity("400m", memory=True)

    def test_real_measurements_capacity_readiness_and_oom_are_projected(self):
        result, calls = collect()
        self.assertEqual(result["observed_at"], "2026-10-25T06:00:00Z")
        self.assertEqual(result["summary"]["nodes_ready"], 1)
        self.assertEqual(result["summary"]["pods_ready"], 1)
        self.assertEqual(result["summary"]["restarts"], 2)
        self.assertEqual(result["summary"]["cpu_usage_cores"], .25)
        self.assertEqual(result["summary"]["pod_cpu_usage_cores"], .125)
        self.assertEqual(result["summary"]["memory_usage_bytes"], 1024 ** 3)
        self.assertEqual(result["summary"]["cpu_allocatable_cores"], 3.9)
        self.assertEqual(result["nodes"][0]["cpu_capacity_cores"], 4)
        self.assertEqual(result["nodes"][0]["metrics_timestamp"], "2026-10-25T05:59:48Z")
        self.assertEqual(result["nodes"][0]["metrics_window_seconds"], 30.125)
        self.assertEqual(result["pods"][0]["metrics_window_seconds"], 60)
        self.assertEqual(result["pods"][0]["reason"], "OOMKilled")
        self.assertEqual(result["events"][0]["object"], "Pod/frontend-a")
        self.assertEqual(len(calls), 5)
        for command, kwargs in calls:
            self.assertEqual(command[:5], ["kubectl", "--context", "kind-boutique", "--request-timeout=8s", "get"])
            self.assertLessEqual(kwargs["timeout"], 10)
            self.assertNotIn("apply", command)
            self.assertNotIn("patch", command)

    def test_raw_specs_messages_env_and_arbitrary_stderr_are_never_published(self):
        result, _ = collect(failures={"node_metrics": "Forbidden: credential=DO-NOT-PUBLISH"})
        encoded = json.dumps(result)
        self.assertNotIn("DO-NOT-PUBLISH", encoded)
        self.assertNotIn("PASSWORD", encoded)
        self.assertNotIn("annotations", encoded)
        self.assertNotIn('"spec"', encoded)
        self.assertNotIn('"message"', encoded)
        self.assertEqual(result["sources"]["node_metrics"]["error_code"], "PERMISSION_DENIED")

    def test_permissions_do_not_hide_available_namespace_metrics(self):
        result, _ = collect(failures={"nodes": "Forbidden: cannot list nodes"})
        self.assertIsNone(result["summary"]["nodes_total"])
        self.assertIsNone(result["summary"]["cpu_allocatable_cores"])
        self.assertIsNone(result["summary"]["cpu_usage_cores"])
        self.assertEqual(result["summary"]["pods_ready"], 1)
        self.assertEqual(result["summary"]["pod_cpu_usage_cores"], .125)

    def test_missing_metrics_server_is_unknown_instead_of_zero(self):
        result, _ = collect(failures={"node_metrics": "Error from server (NotFound): requested resource not found",
                                     "pod_metrics": "Error from server (NotFound)"})
        self.assertEqual(result["sources"]["pod_metrics"]["error_code"], "API_UNAVAILABLE")
        self.assertIsNone(result["summary"]["cpu_usage_cores"])
        self.assertIsNone(result["summary"]["pod_memory_usage_bytes"])
        self.assertIsNone(result["pods"][0]["memory_usage_bytes"])
        self.assertEqual(result["summary"]["pods_total"], 1)

    def test_partial_running_pod_metrics_are_explicitly_incomplete(self):
        docs = resources()
        second = deepcopy(docs["pods"][0])
        second["metadata"]["name"] = "cartservice-b"
        docs["pods"].append(second)
        result, _ = collect(docs)
        self.assertEqual(result["sources"]["pod_metrics"]["status"], "available")
        self.assertTrue(result["sources"]["pod_metrics"]["partial"])
        self.assertEqual(result["sources"]["pod_metrics"]["missing_items"], 1)
        self.assertIsNone(result["summary"]["pod_cpu_usage_cores"])
        self.assertIsNone(result["pods"][1]["cpu_usage_cores"])

    def test_missing_container_measurement_does_not_understate_pod_usage(self):
        docs = resources()
        docs["pods"][0]["spec"]["containers"].append({"name": "sidecar"})
        result, _ = collect(docs)
        self.assertIsNone(result["pods"][0]["cpu_usage_cores"])
        self.assertTrue(result["sources"]["pod_metrics"]["partial"])

    def test_wrong_container_or_duplicate_metrics_cannot_complete_a_pod_measurement(self):
        docs = resources()
        docs["pod_metrics"][0]["containers"][0]["name"] = "unrelated"
        result, _ = collect(docs)
        self.assertIsNone(result["pods"][0]["cpu_usage_cores"])
        docs = resources()
        docs["pod_metrics"][0]["containers"].append(deepcopy(docs["pod_metrics"][0]["containers"][0]))
        result, _ = collect(docs)
        self.assertIsNone(result["pods"][0]["cpu_usage_cores"])

    def test_invalid_resource_measurements_remain_null(self):
        docs = resources()
        docs["nodes"][0]["status"]["allocatable"]["cpu"] = "many"
        docs["node_metrics"][0]["usage"]["cpu"] = "NaN"
        docs["pod_metrics"][0]["containers"][0]["usage"]["memory"] = "-1Mi"
        result, _ = collect(docs)
        self.assertIsNone(result["summary"]["cpu_allocatable_cores"])
        self.assertIsNone(result["summary"]["cpu_usage_cores"])
        self.assertIsNone(result["pods"][0]["memory_usage_bytes"])
        self.assertTrue(result["sources"]["node_metrics"]["partial"])

    def test_all_failed_sources_keep_counts_unknown(self):
        result, _ = collect(failures={name: "Forbidden: DO-NOT-PUBLISH" for name in SOURCE_NAMES})
        self.assertTrue(all(value is None for value in result["summary"].values()))
        self.assertEqual(result["nodes"], [])
        self.assertEqual(result["pods"], [])

    def test_successful_empty_namespace_is_known_zero(self):
        result, _ = collect({name: [] for name in SOURCE_NAMES})
        self.assertEqual(result["summary"]["pods_total"], 0)
        self.assertEqual(result["summary"]["pods_ready"], 0)
        self.assertEqual(result["summary"]["restarts"], 0)

    def test_timeouts_and_invalid_lists_are_sanitized(self):
        result, _ = collect(failures={"nodes": subprocess.TimeoutExpired(["kubectl", "SECRET"], 10),
                                     "pods": FileNotFoundError("SECRET")})
        self.assertEqual(result["sources"]["nodes"]["error_code"], "TIMEOUT")
        self.assertEqual(result["sources"]["pods"]["error_code"], "KUBECTL_UNAVAILABLE")
        self.assertNotIn("SECRET", json.dumps(result))
        result = collect_cluster_snapshot(context="ctx", cluster_id="lab", namespace="boutique",
                                         runner=lambda *a, **k: subprocess.CompletedProcess([], 0, '{"items":{}}', ""))
        self.assertEqual(result["sources"]["pods"]["error_code"], "INVALID_RESPONSE")

    def test_truncated_lists_are_bounded_and_not_reported_as_complete_totals(self):
        docs = resources()
        docs["pods"] = [dict(deepcopy(docs["pods"][0]), metadata={"name": f"pod-{index}", "namespace": "boutique"})
                        for index in range(LIST_LIMITS["pods"] + 2)]
        result, _ = collect(docs)
        self.assertEqual(len(result["pods"]), LIST_LIMITS["pods"])
        self.assertEqual(result["truncation"]["pods"], 2)
        self.assertTrue(result["sources"]["pods"]["partial"])
        self.assertIsNone(result["summary"]["pods_total"])

    def test_failed_deployment_observation_does_not_prevent_cluster_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "telemetry.sqlite3"
            cluster, _ = collect()
            arguments = ["observe_dashboard.py", "--context", "ctx", "--cluster-id", "lab",
                         "--namespace", "boutique", "--db", str(Path(directory) / "coordinator.sqlite3"),
                         "--telemetry", str(telemetry), "--once"]
            with patch("sys.argv", arguments), patch("scripts.observe_dashboard.collect_once", side_effect=RuntimeError("source error")), \
                    patch("scripts.observe_dashboard.collect_cluster_snapshot", return_value=cluster) as cluster_reader, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(collector_main(), 1)
            cluster_reader.assert_called_once_with(context="ctx", cluster_id="lab", namespace="boutique")
            result = snapshot(Path(directory) / "absent.sqlite3", telemetry)
            self.assertEqual(len(result["cluster_history"]), 1)
            self.assertEqual(result["cluster_history"][0]["summary"]["pods_ready"], 1)

    def test_cluster_snapshot_has_separate_table_and_newest_bounded_history(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "telemetry.sqlite3"
            coordinator = Path(directory) / "absent.sqlite3"
            cluster, _ = collect()
            for index in range(CLUSTER_HISTORY_LIMIT + 2):
                cluster["summary"]["restarts"] = index
                emit_cluster_snapshot(telemetry, cluster)
            result = snapshot(coordinator, telemetry)
            self.assertTrue(result["sources"]["cluster_metrics"]["connected"])
            self.assertFalse(result["sources"]["telemetry"]["connected"])
            self.assertEqual(len(result["cluster_history"]), CLUSTER_HISTORY_LIMIT)
            self.assertEqual(result["cluster_history"][0]["summary"]["restarts"], CLUSTER_HISTORY_LIMIT + 1)
            with closing(sqlite3.connect(telemetry)) as db:
                self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='telemetry'").fetchone())
            self.assertFalse(coordinator.exists())

    def test_old_journal_is_read_without_migration_and_cluster_source_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            telemetry = Path(directory) / "telemetry.sqlite3"
            emit_event(telemetry, component=2, event_type="heartbeat", summary="Existing journal")
            before = telemetry.read_bytes()
            result = snapshot(Path(directory) / "absent.sqlite3", telemetry)
            self.assertEqual(telemetry.read_bytes(), before)
            self.assertTrue(result["sources"]["telemetry"]["connected"])
            self.assertFalse(result["sources"]["cluster_metrics"]["connected"])
            self.assertEqual(result["cluster_history"], [])

    def test_cluster_transport_rejects_unbounded_or_raw_top_level_payloads(self):
        cluster, _ = collect()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            with self.assertRaises(ValueError):
                emit_cluster_snapshot(path, {**cluster, "spec": {"secret": "NEVER"}})
            cluster["summary"]["unexpected"] = "x" * (2 * 1024 * 1024)
            with self.assertRaises(ValueError):
                emit_cluster_snapshot(path, cluster)
            self.assertFalse(path.exists())


def owned_pod(name, uid, containers=("server",)):
    """An Observer observation Pod: name, UID and the containers it reports."""
    return {"metadata": {"name": name, "uid": uid},
            "status": {"phase": "Running", "containerStatuses": [
                {"name": item, "ready": True, "restartCount": 0, "state": {"running": {}}} for item in containers]}}


def pod_metrics(name, *containers, namespace="sentinel-boutique-connected", timestamp="2026-10-25T05:59:45Z"):
    return {"metadata": {"name": name, "namespace": namespace}, "timestamp": timestamp, "window": "15s",
            "containers": [{"name": item, "usage": {"cpu": cpu, "memory": memory}} for item, cpu, memory in containers]}


class MetricsBridgeTests(unittest.TestCase):
    """runtime/metrics_bridge.py: metrics.k8s.io usage into Observer gauge points, exactly or not at all."""

    NAMESPACE = "sentinel-boutique-connected"

    def convert(self, items, pods):
        from runtime.metrics_bridge import convert
        return convert(items, pods, namespace=self.NAMESPACE, service="shippingservice")

    @staticmethod
    def points(document):
        result = {}
        for resource in document["resourceMetrics"]:
            uid = next(item["value"]["stringValue"] for item in resource["resource"]["attributes"]
                       if item["key"] == "k8s.pod.uid")
            for metric in resource["scopeMetrics"][0]["metrics"]:
                result[(uid, metric["name"], metric["unit"])] = metric["gauge"]["dataPoints"][0]
        return result

    def test_quantities_convert_exactly_to_cores_and_bytes(self):
        cases = [("250m", "64Mi", 0.25, 67108864), ("180000000n", "1Ki", 0.18, 1024),
                 ("1", "1048576", 1.0, 1048576), ("123456789n", "0", 0.123456789, 0), ("2500u", "1M", 0.0025, 1000000)]
        for cpu, memory, cores, octets in cases:
            with self.subTest(cpu=cpu, memory=memory):
                document, report = self.convert([pod_metrics("ship-a", ("server", cpu, memory))],
                                                [owned_pod("ship-a", "uid-a")])
                points = self.points(document)
                self.assertTrue(report["complete"])
                self.assertEqual(points[("uid-a", "k8s.pod.cpu.usage", "cores")]["asDouble"], cores)
                self.assertEqual(points[("uid-a", "k8s.pod.memory.working_set", "By")]["asInt"], str(octets))
        # Containers sum in exact decimals before one conversion: 0.1 + 0.2 is 0.3, not 0.30000000000000004.
        document, _ = self.convert([pod_metrics("ship-a", ("server", "100m", "1Mi"), ("sidecar", "200m", "1Mi"))],
                                   [owned_pod("ship-a", "uid-a", ("server", "sidecar"))])
        points = self.points(document)
        self.assertEqual(points[("uid-a", "k8s.pod.cpu.usage", "cores")]["asDouble"], 0.3)
        self.assertEqual(points[("uid-a", "k8s.pod.memory.working_set", "By")]["asInt"], str(2 * 1048576))

    def test_points_are_gauges_stamped_with_the_owned_uid_and_metrics_time(self):
        document, _ = self.convert([pod_metrics("ship-a", ("server", "5m", "8Mi"))], [owned_pod("ship-a", "uid-a")])
        resource = document["resourceMetrics"][0]
        attributes = {item["key"]: item["value"]["stringValue"] for item in resource["resource"]["attributes"]}
        self.assertEqual(attributes, {"k8s.namespace.name": self.NAMESPACE, "k8s.pod.uid": "uid-a",
                                      "service.name": "shippingservice"})
        for metric in resource["scopeMetrics"][0]["metrics"]:
            self.assertEqual(set(metric), {"name", "unit", "gauge"})
            self.assertEqual(metric["gauge"]["dataPoints"][0]["timeUnixNano"],
                             str(int(datetime(2026, 10, 25, 5, 59, 45, tzinfo=timezone.utc).timestamp()) * 10 ** 9))

    def test_pods_not_owned_by_the_target_are_dropped(self):
        items = [pod_metrics("ship-a", ("server", "5m", "8Mi")), pod_metrics("frontend-x", ("server", "900m", "8Mi")),
                 pod_metrics("ship-a", ("server", "5m", "8Mi"), namespace="other")]
        document, report = self.convert(items, [owned_pod("ship-a", "uid-a")])
        self.assertEqual(report["foreign_items_dropped"], 2)
        self.assertEqual({key[0] for key in self.points(document)}, {"uid-a"})

    def test_incomplete_metrics_are_left_out_never_zero(self):
        cases = {
            "missing": [],
            "container_coverage": [pod_metrics("ship-a", ("other", "5m", "8Mi"))],
            "invalid_quantity": [pod_metrics("ship-a", ("server", "five", "8Mi"))],
            "no_container_metrics": [{**pod_metrics("ship-a"), "containers": []}],
            "invalid_timestamp": [pod_metrics("ship-a", ("server", "5m", "8Mi"), timestamp="2026-10-25T05:59:45")],
        }
        for reason, items in cases.items():
            with self.subTest(reason=reason):
                document, report = self.convert(items, [owned_pod("ship-a", "uid-a")])
                self.assertEqual(document, {"resourceMetrics": []})
                self.assertFalse(report["complete"])
                self.assertEqual(report["incomplete"], {"ship-a": reason})
        # A fractional byte count is unknown, not rounded down.
        document, report = self.convert([pod_metrics("ship-a", ("server", "5m", "1.5"))], [owned_pod("ship-a", "uid-a")])
        self.assertEqual((document["resourceMetrics"], report["incomplete"]), ([], {"ship-a": "invalid_quantity"}))
        # One owned Pod with metrics and one without: only the first gets points, and coverage is incomplete.
        document, report = self.convert([pod_metrics("ship-a", ("server", "5m", "8Mi"))],
                                        [owned_pod("ship-a", "uid-a"), owned_pod("ship-b", "uid-b")])
        self.assertEqual((report["pods_with_metrics"], report["complete"]), (1, False))

    def test_malformed_requests_are_refused(self):
        from runtime.metrics_bridge import MetricsBridgeError, convert, pod_metrics_path
        with self.assertRaises(MetricsBridgeError):
            convert(None, [], namespace=self.NAMESPACE, service="shippingservice")
        with self.assertRaises(MetricsBridgeError):
            convert([], [], namespace=self.NAMESPACE, service=" ")
        with self.assertRaises(MetricsBridgeError):
            self.convert([pod_metrics("ship-a", ("server", "5m", "8Mi"))] * 2, [owned_pod("ship-a", "uid-a")])
        for namespace in ("", "a/b", None):
            with self.subTest(namespace=namespace), self.assertRaises(MetricsBridgeError):
                pod_metrics_path(namespace)
        self.assertEqual(pod_metrics_path(self.NAMESPACE),
                         "/apis/metrics.k8s.io/v1beta1/namespaces/sentinel-boutique-connected/pods")


if __name__ == "__main__":
    unittest.main()
