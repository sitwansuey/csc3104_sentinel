"""Offline coverage for fault scope, evidence quality and cleanup on failure."""

from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness.chaos import (
    APPLICATIONS, CLUSTER, CONTEXT, FAULTS, SUPPORTED, UNSUPPORTED, Cluster, injected, scenario, validate_scope,
    workload_manifest,
)
from experiments.boutique.harness.collection import metric_rows, namespace_summary, utc
from experiments.boutique.harness.chaos_demo import (
    OWNER, arguments, converged, healthy_window, main, owned_remove, run_case, traffic_resources,
)
from experiments.boutique.traffic.storefront import StorefrontProbe


def status(inject=True):
    return {"status": {"conditions": [{"type": "Selected", "status": "True"},
            {"type": "AllInjected", "status": "True" if inject else "False"}],
            "experiment": {"containerRecords": [{"phase": "Injected" if inject else "Not Injected", "id": "pod-a"}]}}}


class ScenarioTests(unittest.TestCase):
    def test_six_faults_have_bounded_service_selectors(self):
        expected = ["StressChaos", "StressChaos", "PodChaos", "NetworkChaos", "HTTPChaos", "DNSChaos"]
        for fault, kind in zip(FAULTS, expected):
            doc = scenario(fault, CLUSTER, "case", 30)
            self.assertEqual(doc["kind"], kind)
            self.assertEqual(doc["spec"]["selector"]["namespaces"], [CLUSTER])
            self.assertIn(doc["spec"]["selector"]["labelSelectors"]["app"], APPLICATIONS)
            self.assertEqual(doc["spec"]["duration"], "30s")
            self.assertEqual(doc["spec"]["mode"], "one")

    def test_http_targets_real_http_port_and_excludes_health_checks(self):
        spec = scenario("http", CLUSTER, "case", 30)["spec"]
        self.assertEqual((spec["port"], spec["path"]), (8080, "/"))
        with self.assertRaises(ValueError):
            scenario("http", CLUSTER, "case", 30, "shippingservice")

    def test_network_delay_uses_a_direction_admitted_without_a_target(self):
        # Chaos Mesh 2.8.2 rejects netem 'from'/'both' when spec.target is empty.
        spec = scenario("network", CLUSTER, "case", 30)["spec"]
        self.assertEqual(spec["direction"], "to")
        self.assertNotIn("target", spec)

    def test_unsupported_dns_is_refused_before_cluster_work(self):
        self.assertEqual(set(UNSUPPORTED), {"dns"})
        for argv in (["--faults", "dns"], ["--faults", "cpu", "dns"]):
            with patch("sys.stderr", new=io.StringIO()) as error, self.assertRaises(SystemExit):
                arguments(argv)
            self.assertIn("read-only", error.getvalue())
        _, config = arguments(["--faults", "all"])
        self.assertEqual(config["faults"], list(SUPPORTED))
        self.assertNotIn("dns", config["faults"])
        self.assertIn("http", config["faults"])

    def test_default_batch_runs_only_faults_this_lab_can_inject(self):
        _, config = arguments([])
        self.assertEqual(config["faults"], ["cpu", "memory", "pod", "network"])
        self.assertEqual(config["settle_timeout_seconds"], 90)
        _, config = arguments(["--settle-timeout-seconds", "0"])
        self.assertEqual(config["settle_timeout_seconds"], 0)
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit):
            arguments(["--settle-timeout-seconds", "601"])

    def test_scope_and_invalid_duration_rejected(self):
        for context, namespace in [("production", CLUSTER), (CONTEXT, "kube-system")]:
            with self.assertRaises(ValueError):
                validate_scope(context, namespace)
        for duration in [0, -1, 3601, True]:
            with self.assertRaises(ValueError):
                scenario("cpu", CLUSTER, "case", duration)

    def test_confirmation_requires_actual_selected_records(self):
        self.assertTrue(injected(status()))
        self.assertFalse(injected(status(False)))
        doc = status()
        doc["status"]["experiment"]["containerRecords"] = []
        self.assertFalse(injected(doc))

    def test_cluster_service_is_opt_in_and_restricted(self):
        url = f"http://frontend.{CLUSTER}.svc.cluster.local:80"
        with self.assertRaises(ValueError):
            StorefrontProbe(url)
        self.assertEqual(StorefrontProbe(url, allow_cluster_service=True).base_url, url)
        for url in ["http://example.com:80", "http://frontend.evil.com:80", "https://frontend.lab.svc.cluster.local:80"]:
            with self.assertRaises(ValueError):
                StorefrontProbe(url, allow_cluster_service=True)

    def test_worker_has_no_cluster_credentials_and_uses_service_network(self):
        _, config = arguments([])
        cm, pod = traffic_resources(CLUSTER, "run-a", config)
        self.assertFalse(pod["spec"]["automountServiceAccountToken"])
        self.assertNotIn("app", pod["metadata"]["labels"])
        env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
        self.assertEqual(env["STOREFRONT_URL"], f"http://frontend.{CLUSTER}.svc.cluster.local:80")
        self.assertIn("worker.py", cm["data"])


class EvidenceTests(unittest.TestCase):
    def test_metrics_are_actual_usage_and_namespace_filtered(self):
        data = {"pods": [{"podRef": {"namespace": CLUSTER, "name": "p"}, "containers": [
            {"name": "server", "cpu": {"usageNanoCores": 250000000}, "memory": {"workingSetBytes": 123}}]},
            {"podRef": {"namespace": "other"}}]}
        selected = namespace_summary(data, CLUSTER)
        self.assertEqual(len(selected["pods"]), 1)
        row = metric_rows(selected)[0]
        self.assertEqual(row["cpu_cores"], .25)
        self.assertEqual(row["memory_working_set_bytes"], 123)
        self.assertIsNone(row["memory_usage_bytes"])

    def test_missing_failed_and_unknown_traffic_are_not_healthy(self):
        row = {"started_at": "02", "completed_at": "03", "journey": {"passed": True}}
        self.assertTrue(healthy_window([row], "01", "04"))
        self.assertFalse(healthy_window([], "01", "04"))
        for value in [False, None]:
            row["journey"]["passed"] = value
            self.assertFalse(healthy_window([row], "01", "04"))

    def test_cleanup_never_deletes_foreign_resource(self):
        cluster = Mock()
        cluster.call.return_value = json.dumps({"metadata": {"labels": {OWNER: "other"}}})
        with self.assertRaises(RuntimeError):
            owned_remove(cluster, "PodChaos", "case", "mine")
        cluster.remove.assert_not_called()

    def test_dry_run_has_no_cluster_access(self):
        with tempfile.TemporaryDirectory() as temp, patch("experiments.boutique.harness.chaos_demo.Cluster") as cluster:
            self.assertEqual(main(["--dry-run", "--runs-dir", temp]), 0)
            cluster.assert_not_called()
            self.assertEqual(len(list(Path(temp).glob("*/manifests/*.json"))), 4)

    def test_converged_needs_the_latest_journeys_after_the_rollout(self):
        def row(start, passed):
            return {"started_at": start, "completed_at": start, "journey": {"passed": passed}}
        before = [row("01", False), row("02", True)]
        self.assertFalse(converged(before, "05"))
        self.assertFalse(converged(before + [row("06", True), row("07", True)], "05"))
        self.assertFalse(converged(before + [row("06", True), row("07", True), row("08", False)], "05"))
        self.assertFalse(converged(before + [row("06", True), row("07", True), row("08", None)], "05"))
        failing_then_passing = [row("06", False), row("07", True), row("08", True), row("09", True)]
        self.assertTrue(converged(before + failing_then_passing, "05"))
        self.assertTrue(converged(list(reversed(before + failing_then_passing)), "05"))


class LifecycleTests(unittest.TestCase):
    def execute(self, temp, *, confirm=True, collector_errors=0, cleanup_error=False, traffic=None, stamp=None):
        _, config = arguments(["--fault-seconds", "5"])
        config["operator_gates"] = False
        clock = [0]
        cluster = Mock(namespace=CLUSTER)
        cluster.create.side_effect = lambda document: deepcopy(document)
        cluster.get.side_effect = lambda kind, *args: {"items": []} if kind == "deployments" else status(confirm)
        collector = Mock(errors=collector_errors, samples=3, overruns=0, log_gaps=0)
        row = {"started_at": "02", "completed_at": "03", "journey": {"passed": True}}
        environment = {"kubernetes": {"gitVersion": "test"}, "chaos_mesh_version": "test"}

        def advance(seconds):
            clock[0] += seconds

        def remove(_, kind, *args):
            if cleanup_error and kind == "StressChaos":
                raise RuntimeError("finalizer stuck")

        prefix = "experiments.boutique.harness.chaos_demo."
        with patch(prefix + "Collector", return_value=collector), \
                patch(prefix + "source_revision", return_value="test"), \
                patch(prefix + "deployments_ready", return_value=True), \
                patch(prefix + "read_traffic", side_effect=traffic or (lambda *args: [row])), \
                patch(prefix + "utc", side_effect=stamp or utc), \
                patch(prefix + "healthy_window", return_value=True), \
                patch(prefix + "window_rows", return_value=[row]), \
                patch(prefix + "time.monotonic", side_effect=lambda: clock[0]), \
                patch(prefix + "wait_seconds", side_effect=advance), \
                patch(prefix + "owned_remove", side_effect=remove) as cleanup:
            result = run_case(cluster, config, "cpu", Path(temp) / "run-a", environment)
        return result, cleanup, cluster

    def test_confirmed_run_records_outcome_and_separate_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            result, cleanup, _ = self.execute(temp)
            self.assertEqual(result["status"], "complete")
            self.assertTrue(result["injection_confirmed"])
            self.assertTrue((Path(temp) / "run-a/ground-truth.json").exists())
            self.assertTrue((Path(temp) / "run-a/checksums.json").exists())
            self.assertGreaterEqual(cleanup.call_count, 3)

    def test_recovery_window_waits_for_callers_to_settle(self):
        polls = []

        def traffic(*args):
            polls.append(1)
            # Two failing journeys after the rollout, then passing journeys.
            passed = len(polls) > 3
            return [{"started_at": f"T{index:03d}", "completed_at": f"T{index:03d}",
                     "journey": {"passed": passed or index > 2}} for index in range(1, 2 + len(polls))]
        with tempfile.TemporaryDirectory() as temp:
            result, _, _ = self.execute(temp, traffic=traffic, stamp=lambda: "T000")
            timeline = [json.loads(line)["event"] for line in
                        (Path(temp) / "run-a/timeline.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["status"], "complete")
        self.assertTrue(result["settle"]["settled"])
        self.assertEqual(result["settle"]["timeout_seconds"], 90)
        self.assertLess(timeline.index("settle_finished"), timeline.index("recovery_window_started"))

    def test_settle_timeout_still_measures_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _, _ = self.execute(temp)
        # No journey after the rollout ever passes: the bounded wait gives up and the
        # normal recovery-window rule decides (patched healthy here).
        self.assertFalse(result["settle"]["settled"])
        self.assertEqual(result["status"], "complete")

    def test_unconfirmed_injection_is_excluded_and_cleaned(self):
        with tempfile.TemporaryDirectory() as temp:
            result, cleanup, _ = self.execute(temp, confirm=False)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["injection_confirmed"])
            self.assertIn("did not confirm", result["errors"][0])
            self.assertGreaterEqual(cleanup.call_count, 3)

    def test_missing_telemetry_is_excluded(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _, _ = self.execute(temp, collector_errors=1)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["telemetry_complete"])

    def test_failed_finalizer_cleanup_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            result, _, _ = self.execute(temp, cleanup_error=True)
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["cleanup_confirmed"])
            self.assertTrue(any("finalizer stuck" in e for e in result["errors"]))


if __name__ == "__main__":
    unittest.main()
