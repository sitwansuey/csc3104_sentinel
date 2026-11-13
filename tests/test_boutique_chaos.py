"""Offline coverage for fault scope, evidence quality and cleanup on failure."""

from copy import deepcopy
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from services.coordinator.core import handle

from experiments.boutique.harness.chaos import (
    APPLICATIONS, CLUSTER, CONTEXT, FAULTS, SUPPORTED, UNSUPPORTED, Cluster, injected, scenario, validate_scope,
    workload_manifest,
)
from experiments.boutique.harness.collection import metric_rows, namespace_summary, utc
from experiments.boutique.harness.chaos_demo import (
    OWNER, arguments, converged, healthy_window, main, owned_remove, run_case, traffic_resources,
)
from experiments.boutique.traffic.storefront import StorefrontProbe

ROOT = Path(__file__).resolve().parents[1]


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


# P4.03: Chaos Mesh in the connected lab, attribution, forced refusals and the true-negative control.

CONNECTED_CONTEXT, CONNECTED_NAMESPACE = "kind-sentinel-boutique-connected", "sentinel-boutique-connected"


class ChaosInstallTests(unittest.TestCase):
    """ensure_chaos_mesh and ensure_metrics_server: install only, the connected lab only, never sentinel-lab."""

    def run_install(self, cluster, function, *arguments, nodes=("sentinel-boutique-connected-control-plane",),
                    commands=None):
        commands = [] if commands is None else commands

        def command(args, **kwargs):
            commands.append(list(args))
            if args[-4:-2] == ["get", "nodes"]:
                return json.dumps({"items": [{"metadata": {"name": name}} for name in nodes]})
            return ""
        with patch.object(Cluster, "command", side_effect=command), patch("shutil.which", return_value="/usr/bin/tool"):
            function(cluster, *arguments)
        return commands

    def test_installs_refuse_a_context_that_does_not_reach_the_connected_lab(self):
        from experiments.boutique.harness.chaos import ensure_chaos_mesh
        from experiments.boutique.harness.chaos_recovery import ensure_metrics_server
        digest = "sha256:" + "c" * 64
        for nodes in ((), ("sentinel-boutique-chaos-control-plane",),
                      ("sentinel-boutique-connected-control-plane", "sentinel-boutique-connected-worker")):
            for function, extra in ((ensure_chaos_mesh, ()), (ensure_metrics_server, (digest,))):
                with self.subTest(nodes=nodes, install=function.__name__):
                    commands = []
                    with self.assertRaisesRegex(RuntimeError, "does not point to the single-node connected lab"):
                        self.run_install(Cluster(CONNECTED_CONTEXT, CONNECTED_NAMESPACE), function, *extra,
                                         nodes=nodes, commands=commands)
                    # Only the node read ran: no helm, no annotation.
                    self.assertEqual([command[-4:] for command in commands], [["get", "nodes", "-o", "json"]])

    def test_ensure_chaos_mesh_runs_only_the_pinned_helm_install_and_the_annotation(self):
        from experiments.boutique.harness.chaos import CHAOS_VERSION, ensure_chaos_mesh
        cluster = Cluster(CONNECTED_CONTEXT, CONNECTED_NAMESPACE)
        commands = self.run_install(cluster, ensure_chaos_mesh)
        self.assertEqual([command[0] for command in commands], ["kubectl", "helm", "kubectl"])
        nodes, helm, annotate = commands
        self.assertEqual(nodes[nodes.index("--request-timeout=20s") + 1:], ["get", "nodes", "-o", "json"])
        self.assertEqual(helm[:5], ["helm", "upgrade", "--install", "chaos-mesh", "chaos-mesh"])
        self.assertEqual(helm[helm.index("--version") + 1], CHAOS_VERSION)
        self.assertEqual(CHAOS_VERSION, "2.8.2")
        self.assertEqual(helm[helm.index("--kube-context") + 1], CONNECTED_CONTEXT)
        self.assertTrue(helm[helm.index("--values") + 1].endswith("values.yaml"))
        self.assertEqual(annotate[annotate.index("--request-timeout=20s") + 1:],
                         ["annotate", "namespace", CONNECTED_NAMESPACE, "chaos-mesh.org/inject=enabled", "--overwrite"])
        self.assertEqual(annotate[1:3], ["--context", CONNECTED_CONTEXT])
        flat = " ".join(" ".join(command) for command in commands)
        for forbidden in ("kind ", "create cluster", "delete", "apply", "uninstall"):
            self.assertNotIn(forbidden, flat)
        values = (ROOT / "deploy/chaos-mesh/values.yaml").read_text(encoding="utf-8")
        self.assertIn("enableFilterNamespace: true", values)

    def test_ensure_chaos_mesh_refuses_every_other_context_before_any_command(self):
        from experiments.boutique.harness.chaos import ensure_chaos_mesh
        for context, namespace, message in (
                (CONTEXT, CLUSTER, "limited to kind-sentinel-boutique-connected"),
                ("kind-sentinel-boutique-lab", "sentinel-boutique", "limited to"),
                (CONNECTED_CONTEXT, "sentinel-boutique-other", "limited to"),
                ("kind-sentinel-boutique-sentinel-lab", CONNECTED_NAMESPACE, "never installed into or aimed at sentinel-lab"),
                (CONNECTED_CONTEXT, "sentinel-boutique-sentinel-lab", "sentinel-lab")):
            with self.subTest(context=context, namespace=namespace):
                cluster = Cluster(context, namespace)
                with self.assertRaisesRegex(ValueError, message):
                    self.assertEqual(self.run_install(cluster, ensure_chaos_mesh), [])
        with self.assertRaisesRegex(ValueError, "kind-sentinel-boutique"):
            Cluster("kind-sentinel-lab", "sentinel-demo")

    def test_ensure_metrics_server_pins_the_chart_and_the_image_digest(self):
        from experiments.boutique.harness.chaos_recovery import METRICS_SERVER_CHART, ensure_metrics_server
        digest = "sha256:" + "c" * 64
        commands = self.run_install(Cluster(CONNECTED_CONTEXT, CONNECTED_NAMESPACE), ensure_metrics_server, digest)
        self.assertEqual(len(commands), 2)
        self.assertEqual(commands[0][-4:], ["get", "nodes", "-o", "json"])
        helm = commands[1]
        self.assertEqual(helm[:5], ["helm", "upgrade", "--install", "metrics-server", "metrics-server"])
        self.assertEqual(helm[helm.index("--version") + 1], METRICS_SERVER_CHART)
        self.assertEqual(helm[helm.index("--set-string") + 1], "image.tag=v0.7.2@" + digest)
        self.assertEqual(helm[helm.index("--namespace") + 1], "kube-system")
        values = (ROOT / "deploy/metrics-server/values.yaml").read_text(encoding="utf-8")
        self.assertIn("--kubelet-insecure-tls", values)
        for bad in ("v0.7.2", "sha256:" + "C" * 64, "latest", None):
            with self.subTest(digest=bad), self.assertRaisesRegex(ValueError, "pinned by digest"):
                self.run_install(Cluster(CONNECTED_CONTEXT, CONNECTED_NAMESPACE), ensure_metrics_server, bad)
        with self.assertRaisesRegex(ValueError, "limited to"):
            self.run_install(Cluster(CONTEXT, CLUSTER), ensure_metrics_server, digest)

    def test_preflight_only_reads(self):
        from experiments.boutique.harness.chaos_recovery import preflight
        cluster = Cluster(CONNECTED_CONTEXT, CONNECTED_NAMESPACE)
        verbs = []

        def kubectl(args, **kwargs):
            verbs.append(args[args.index("--request-timeout=20s") + 1])
            if "--raw" in args:
                return json.dumps({"items": [{}]})
            if "chaos-mesh" in args and "pods" in args:
                ready = {"status": {"containerStatuses": [{"ready": True}]}}
                return json.dumps({"items": [{"metadata": {"name": "chaos-controller-manager-1"}, **ready},
                                             {"metadata": {"name": "chaos-daemon-x"}, **ready}]})
            if "namespace" in args:
                return json.dumps({"metadata": {"annotations": {"chaos-mesh.org/inject": "enabled"}}})
            if "crd" in args:
                return "customresourcedefinition/x"
            return json.dumps({"items": []})
        with patch.object(Cluster, "command", side_effect=kubectl):
            facts = preflight(cluster)
        self.assertEqual(set(verbs), {"get"})
        self.assertEqual(facts["pod_metrics_items"], 1)
        with patch.object(Cluster, "command", side_effect=lambda args, **kwargs: (
                json.dumps({"items": [{"metadata": {"name": "left-over"}}]}) if args[-3:] == ["podchaos", "-o", "json"]
                else kubectl(args))):
            with self.assertRaisesRegex(RuntimeError, "left in the connected namespace"):
                preflight(cluster)
        # No Pod metrics: N3 could never alert and T1 could never prove a negative, so the preflight stops.
        with patch.object(Cluster, "command", side_effect=lambda args, **kwargs: (
                json.dumps({"items": []}) if "--raw" in args else kubectl(args))):
            with self.assertRaisesRegex(RuntimeError, "returned no Pod metrics"):
                preflight(cluster)

    def test_setup_cli_refuses_sentinel_lab_and_a_tag_only_metrics_server(self):
        from experiments.boutique.harness.chaos_recovery import main
        for argv, message in ((["install", "--context", "kind-sentinel-lab"], "sentinel-lab"),
                              (["preflight", "--context", CONTEXT, "--namespace", CLUSTER], "limited to"),
                              (["install"], "--metrics-server-digest"),
                              (["install", "--metrics-server-digest", "v0.7.2"], "--metrics-server-digest")):
            with self.subTest(argv=argv), patch("sys.stderr", new=io.StringIO()) as error, \
                    patch.object(Cluster, "command", side_effect=AssertionError("no command")), \
                    self.assertRaises(SystemExit):
                main(argv)
            self.assertIn(message, error.getvalue())


def chaos_object(*records, uid="chaos-uid-1", phase="Run", namespace="sentinel-demo", owner="run"):
    from experiments.boutique.harness.chaos_demo import OWNER
    return {"apiVersion": "chaos-mesh.org/v1alpha1", "kind": "PodChaos",
            "metadata": {"name": "sentinel-pod-test", "namespace": namespace, "uid": uid, "labels": {OWNER: owner}},
            "spec": {"action": "pod-failure", "mode": "one", "duration": "420s"},
            "status": {"conditions": [{"type": "Selected", "status": "True"}, {"type": "AllInjected", "status": "True"}],
                       "experiment": {"desiredPhase": phase, "containerRecords": [
                           {"id": namespace + "/" + name, "phase": "Injected"} for name in records]}}}


class AttributionTests(unittest.TestCase):
    """A RESTART is Sentinel's repair only when the fault itself cannot have healed the target."""

    CREATED = "2026-11-13T10:00:00Z"

    def setUp(self):
        from experiments.boutique.harness.chaos_recovery import chaos_record
        self.record = chaos_record(chaos_object("demo-api-old"), created_at=self.CREATED, duration_seconds=420,
                                   pods={"demo-api-old": "old-pod-uid"})
        self.outcome = {"status": "RECOVERED", "window_end": "2026-11-13T10:04:00Z"}
        self.pods = [{"name": "demo-api-new", "uid": "new-pod-uid"}]

    def attribute(self, record=None, outcome=None, pods=None, chaos_now="default"):
        from experiments.boutique.harness.chaos_recovery import attribution
        return attribution(record or self.record, outcome or self.outcome,
                           current_pods=self.pods if pods is None else pods,
                           chaos_now=chaos_object("demo-api-old") if chaos_now == "default" else chaos_now)

    def failed(self, proof):
        return [item["name"] for item in proof["checks"] if not item["passed"]]

    def test_a_new_pod_recovered_while_the_chaos_still_runs_is_attributed(self):
        proof = self.attribute()
        self.assertTrue(proof["attributed"], proof)
        self.assertEqual(self.record["expires_at"], "2026-11-13T10:07:00Z")
        self.assertEqual(proof["checks"][2]["detail"]["seconds_before_expiry"], 180.0)

    def test_the_selected_pod_uid_still_running_fails(self):
        proof = self.attribute(pods=[{"name": "demo-api-renamed", "uid": "old-pod-uid"}])
        self.assertEqual(self.failed(proof), ["new_pods_not_selected"])

    def test_a_new_pod_named_in_the_chaos_records_fails(self):
        # Chaos Mesh re-selected the new Pod: its name now appears in containerRecords.
        proof = self.attribute(chaos_now=chaos_object("demo-api-old", "demo-api-new"))
        self.assertEqual(self.failed(proof), ["new_pods_not_selected"])
        proof = self.attribute(pods=[{"name": "demo-api-old", "uid": "recreated-uid"}])
        self.assertEqual(self.failed(proof), ["new_pods_not_selected"])
        self.assertFalse(self.attribute(pods=[])["attributed"])

    def test_recovery_after_expiry_minus_the_margin_fails(self):
        for window_end in ("2026-11-13T10:06:00Z", "2026-11-13T10:06:30Z", "2026-11-13T10:08:00Z"):
            with self.subTest(window_end=window_end):
                proof = self.attribute(outcome={"status": "RECOVERED", "window_end": window_end})
                self.assertEqual(self.failed(proof), ["recovered_before_expiry"])
        self.assertTrue(self.attribute(outcome={"status": "RECOVERED", "window_end": "2026-11-13T10:05:59Z"})["attributed"])
        self.assertEqual(self.failed(self.attribute(outcome={"status": "RECOVERED"})), ["recovered_before_expiry"])

    def test_a_chaos_object_that_is_gone_stopped_or_replaced_fails(self):
        deleting = chaos_object("demo-api-old")
        deleting["metadata"]["deletionTimestamp"] = "2026-11-13T10:03:00Z"
        for chaos_now in (None, chaos_object("demo-api-old", phase="Stop"),
                          chaos_object("demo-api-old", uid="another-object"), deleting):
            with self.subTest(chaos_now=chaos_now and chaos_now["metadata"]):
                self.assertEqual(self.failed(self.attribute(chaos_now=chaos_now)), ["chaos_still_running"])

    def test_an_outcome_other_than_recovered_fails(self):
        proof = self.attribute(outcome={"status": "NOT_RECOVERED", "window_end": "2026-11-13T10:04:00Z"})
        self.assertEqual(self.failed(proof), ["outcome_recovered"])

    def test_record_ids_must_name_pods_in_the_namespace(self):
        from experiments.boutique.harness.chaos_recovery import chaos_record, record_pod_names
        self.assertEqual(record_pod_names(["ns/pod-a", "ns/pod-a/server", "ns/pod-b"], "ns"), ["pod-a", "pod-b"])
        for value in ("pod-a", "other/pod-a", "ns/Pod_A", "ns/a/b/c", ""):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Unexpected Chaos Mesh record id"):
                record_pod_names([value], "ns")
        with self.assertRaisesRegex(RuntimeError, "UID recorded"):
            chaos_record(chaos_object("demo-api-old"), created_at=self.CREATED, duration_seconds=420, pods={})
        with self.assertRaisesRegex(RuntimeError, "UID recorded"):
            chaos_record(chaos_object(), created_at=self.CREATED, duration_seconds=420, pods={})


class TrueNegativeProofTests(unittest.TestCase):
    """No incident is a result only with evidence that the Observer looked and nothing was written."""

    def proof(self, **changes):
        from experiments.boutique.harness.chaos_recovery import true_negative_proof
        facts = {"injected": True, "observations": ["2026-11-13T10:00:00Z", "2026-11-13T10:01:00Z",
                                                    "2026-11-13T10:02:10Z"],
                 "window_seconds": 120, "thresholds_complete": 3, "coordinator_incidents": 0,
                 "coordinator_actions": 0, "executor_actions": 0, "template_sha256": "a" * 64,
                 "baseline_sha256": "a" * 64}
        facts.update(changes)
        return true_negative_proof(**facts)

    def test_full_evidence_passes_with_zero_writes(self):
        proof = self.proof()
        self.assertTrue(proof["passed"])
        self.assertEqual((proof["executor_writes"], proof["observations"], proof["observed_seconds"]), (0, 3, 130.0))

    def test_no_incident_by_omission_never_passes(self):
        for change, check in (({"injected": False}, "fault_injected"),
                              ({"observations": ["2026-11-13T10:00:00Z"]}, "observer_window_covered"),
                              ({"observations": ["2026-11-13T10:00:00Z", "2026-11-13T10:01:00Z"]},
                               "observer_window_covered"),
                              ({"thresholds_complete": 0}, "metric_thresholds_evaluated"),
                              ({"coordinator_incidents": 1}, "no_incident"),
                              ({"coordinator_actions": 1}, "no_coordinator_action"),
                              ({"executor_actions": 1}, "no_executor_action"),
                              ({"template_sha256": "b" * 64}, "template_at_baseline")):
            with self.subTest(change=change):
                proof = self.proof(**change)
                self.assertFalse(proof["passed"])
                self.assertEqual([name for name, passed in proof["checks"].items() if not passed], [check])
        self.assertIsNone(self.proof(executor_actions=1)["executor_writes"])


class FakeChaosCluster:
    """The chaos object as the connected lab's administrator reads it; removal waits for nothing here."""

    def __init__(self, document):
        self.document, self.removed = document, []

    def call(self, *args, **kwargs):
        if args[0] != "get":
            raise AssertionError("unexpected chaos command " + str(args))
        return json.dumps(self.document) if self.document else ""

    def remove(self, kind, name):
        self.removed.append((kind, name))
        self.document = None


class OfflineChaosLab:
    """The real ChaosRecoveryMixin, recovery Experiment, durable case, Coordinator, Guard, approval session and
    Executor core; Kubernetes, the chaos object and the Verifier outcome are fakes (mixed into a TestCase)."""

    scenario_id = "N2"
    fixture = "pod-failure"

    def setUp(self):
        from experiments.boutique.harness import scenarios as catalog
        from experiments.boutique.harness.chaos_recovery import ChaosRecoveryMixin, chaos_record, owner_label
        from experiments.recovery import live as recovery
        from experiments.validation.demo_offline_flow import restart_policy
        from services.observer.core import detect
        from tests.support.executor_fake import FakeDeploymentAPI
        from tests.test_approval_session import real_now, sample, sample_delta

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        delta = sample_delta()
        self.sample, self.real_now = (lambda name: sample(name, delta)), real_now
        observations = self.sample("observations.json")
        self.baseline, self.policy = self.sample("baseline.json"), restart_policy(self.sample("policy.json"))
        self.incident = detect(observations["pod-failure"], self.policy)
        live = observations[self.fixture][-1]
        if self.fixture == "healthy":
            live["deployment"]["spec"]["template"] = deepcopy(self.baseline["pod_template"])

        class Deployments(FakeDeploymentAPI):
            @property
            def stats(self):
                return {"write_attempts": len(self.write_attempts), "writes": len(self.writes)}

        self.cluster = Deployments(live, clock=real_now)
        self.item = catalog.scenario(self.scenario_id)
        self.commands = []
        for patcher in (patch("subprocess.run", side_effect=AssertionError("cluster command attempted")),
                        patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch.object(recovery, "build_adapters", side_effect=self.build_adapters),
                        patch("experiments.boutique.harness.durable_case.RETRY_SECONDS", .01),
                        patch("experiments.boutique.harness.durable_case.POLL_SECONDS", .01)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.chaos = FakeChaosCluster(chaos_object("demo-api-old", owner=owner_label("run")))
        created = recovery.utc(real_now() - timedelta(seconds=30))
        self.record = chaos_record(self.chaos.document, created_at=created, duration_seconds=420,
                                   pods={"demo-api-old": "old-pod-uid"})
        self.lab_type = type("OfflineChaosExperiment", (ChaosRecoveryMixin, recovery.Experiment), {
            "scenario": property(lambda lab: self.item),
            "deployment": lambda lab: self.cluster.read(lab.target)["deployment"],
            "dependency_result": lambda lab, proposal: self.dependencies(proposal),
            "complete_recovery": lambda lab, folder, proposal, execution: self.verified(lab, folder, proposal, execution),
            "chaos_cluster": lambda lab: self.chaos,
            "kubectl": lambda lab, *args, **kwargs: self.kubectl(*args, **kwargs),
            "event": lambda lab, *args, **kwargs: None})

    def dependencies(self, proposal):
        return dict(self.sample("messages/dependency-result.json"), incident_id=proposal["incident_id"],
                    action_id=proposal["action_id"])

    def build_adapters(self, **options):
        from runtime.executor_pipeline import GuardAuthorizer
        from services.executor.core import ExecutionAdapters
        from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
        reader = options["approval_reader"] or (lambda proposal: json.loads(Path(options["approval"]).read_text()))
        authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
                                     dependency_reader=self.dependencies, approval_reader=reader)
        return ExecutionAdapters(ledger=SQLiteExecutionLedger(options["ledger"]),
                                 coordination=LocalTargetCoordinator(options["locks"]), cluster=self.cluster,
                                 authorizer=authorizer, clock=self.real_now)

    def verified(self, lab, folder, proposal, execution):
        """A fixed passing Verifier outcome, saved where the real complete_recovery saves it."""
        from experiments.recovery.live import save, utc
        start = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
        outcome = dict(self.sample("messages/outcome.json"), incident_id=proposal["incident_id"],
                       action_id=proposal["action_id"], window_start=utc(start),
                       window_end=utc(max(self.real_now(), start + timedelta(seconds=1))))
        handle(outcome, lab.store, lab.bus)
        save(folder / "outcome.json", outcome)

    listed_pods = ({"name": "demo-api-new", "uid": "new-pod-uid"},)
    patch_conflicts = 0  # How many administrator patches fail first, as a 409 from a racing status update would.

    def kubectl(self, *args, **kwargs):
        """The administrator's kubectl: Pod listing, the R1 drift injection, the restore patch, rollout status."""
        self.commands.append(args)
        template = self.cluster.snapshot["deployment"]["spec"]["template"]
        stdout = ""
        if args[:2] == ("get", "pods"):
            stdout = json.dumps({"items": [{"metadata": dict(pod)} for pod in self.listed_pods]})
        elif args[:2] == ("set", "env"):
            name, value = args[-1].split("=", 1)
            template["spec"]["containers"][0]["env"].append({"name": name, "value": value})
        elif args[0] == "patch":
            if self.patch_conflicts:
                self.patch_conflicts -= 1
                if not kwargs.get("allow_failure"):
                    raise RuntimeError("kubectl operation failed: patch")
                return SimpleNamespace(returncode=1, stdout="", stderr="Error from server (Conflict)")
            patch_document = json.loads(Path(args[args.index("--patch-file") + 1]).read_text(encoding="utf-8"))
            self.cluster.snapshot["deployment"]["spec"]["template"] = deepcopy(patch_document[-1]["value"])
        elif args[0] != "rollout":
            raise AssertionError("unexpected kubectl " + str(args))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def observe_window(self, lab, *, complete=True, count=4):
        """Observer captures spanning the 120 s detection window, each with or without complete thresholds."""
        from experiments.recovery.live import save, utc
        start = self.real_now() - timedelta(seconds=130)
        for index in range(count):
            save(lab.directory / f"primary-{index}-observation.json",
                 {"captured_at": utc(start + timedelta(seconds=index * 130 / max(count - 1, 1))),
                  "telemetry": {"thresholds_complete": complete}})

    def lab(self, *, expect="recovered", approval="auto-lab"):
        from experiments.recovery.live import save
        lab = self.lab_type(self.root / "run")
        self.addCleanup(lab.store.close)
        self.addCleanup(lab.close_approval_authority)
        lab.target, lab.baseline, lab.policy, lab.expect = self.incident["target"], self.baseline, self.policy, expect
        save(lab.directory / "policy.json", self.policy)
        if approval:
            lab.configure_approval(approval)
        else:
            lab.approval_not_requested("no approval was requested: test")
        lab.chaos_record, lab.chaos_document = deepcopy(self.record), deepcopy(self.chaos.document)
        return lab

    def proof(self, lab, label):
        return json.loads((lab.directory / label / "proof.json").read_text(encoding="utf-8"))

    def result(self, lab):
        return json.loads((lab.directory / "scenario-result.json").read_text(encoding="utf-8"))


class PodFailureRecoveryTests(OfflineChaosLab, unittest.TestCase):
    """N2: the code rule plans RESTART, the Guard sees the restart history, and the repair is attributed."""

    def test_rule_restart_recovers_with_attribution_and_both_forced_actions_are_refused(self):
        from experiments.boutique.harness.chaos_recovery import PLANNING_FOLDER
        lab = self.lab()
        handle(self.incident, lab.store, lab.bus)
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(self.incident)
            lab.after_recovery_scenarios()
        self.assertEqual(lab.store.incident(self.incident["incident_id"])["state"], "RECOVERED")
        result = self.result(lab)
        self.assertEqual((result["actual"], result["decision_source"], result["score"], result["matched_expected"]),
                         ("RESTART", "rule", "correct", True))
        self.assertEqual(result["executor_writes"], 1)
        # Exactly one Kubernetes write in the whole run: the RESTART. Both forced wrong actions wrote nothing.
        self.assertEqual(len(self.cluster.writes), 1)
        proposal = json.loads((lab.directory / PLANNING_FOLDER / "proposal.json").read_text(encoding="utf-8"))
        self.assertEqual(proposal["action"], "RESTART")
        rollback = self.proof(lab, "forced-rollback")
        self.assertEqual((rollback["guard_verdict"], rollback["executor_patch_attempts"], rollback["template_unchanged"]),
                         ("REJECT", 0, True))
        self.assertIn("ROLLBACK_ALREADY_AT_BASELINE", rollback["guard_reason_codes"])
        drift = self.proof(lab, "forced-restart-drift")
        self.assertEqual((drift["guard_verdict"], drift["executor_patch_attempts"]), ("REJECT", 0))
        self.assertIn("RESTART_TEMPLATE_DRIFT", drift["guard_reason_codes"])
        # The run's own RESTART is in the ledger history, so the drift cell's budget is exhausted, not unknown.
        self.assertIn("RESTART_BUDGET_EXHAUSTED", drift["guard_reason_codes"])
        self.assertNotIn("RESTART_BUDGET_UNKNOWN", drift["guard_reason_codes"])
        attribution = json.loads((lab.directory / "attribution.json").read_text(encoding="utf-8"))
        self.assertTrue(attribution["attributed"])
        self.assertEqual(lab.summary["attribution"], {"attributed": True, "failed": []})
        # Cleanup is the run's own chaos object, then the administrator removed the drift.
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])
        self.assertTrue(lab.summary["chaos_cleanup_confirmed"] and lab.summary["drift_cell_restored"])
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"], self.baseline["pod_template"])

    def test_the_guard_reads_the_restart_budget_from_the_executor_ledger(self):
        from services.executor.store import SQLiteExecutionLedger
        lab = self.lab()
        restart = {"action": "RESTART", "target": self.incident["target"]}
        self.assertEqual(lab.guard_state(restart)["restart_history"], [])
        self.assertNotIn("restart_history", lab.guard_state({"action": "ROLLBACK", "target": self.incident["target"]}))
        handle(self.incident, lab.store, lab.bus)
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(self.incident)
        history = lab.guard_state(restart)["restart_history"]
        self.assertEqual(len(history), 1)
        with SQLiteExecutionLedger(lab.directory / "executor.sqlite") as ledger:
            self.assertEqual(history, ledger.restart_history(self.incident["target"]))
        # The Coordinator-side Guard pass saw the budget: its report has RESTART_BUDGET_AVAILABLE, never UNKNOWN.
        reports = [json.loads(row[0]) for row in lab.store.db.execute("SELECT report FROM guard_reports")]
        codes = {check["code"] for report in reports for check in report["checks"]}
        self.assertIn("RESTART_BUDGET_AVAILABLE", codes)
        self.assertNotIn("RESTART_BUDGET_UNKNOWN", codes)

    def test_the_operator_case_gives_the_guard_the_restart_history(self):
        """experiments/recovery/live.py case(): the Coordinator-side Guard reads restart_history for a RESTART,
        so its first answer is the approval request alone, not RESTART_BUDGET_UNKNOWN."""
        lab = self.lab()
        handle(self.incident, lab.store, lab.bus)
        lab.build_proposal = lambda incident, folder, rationale=None: lab.forced_proposal(incident, "RESTART")
        folder, proposal = lab.case("operator-restart", self.incident, coordinator=True)
        self.assertEqual(proposal["action"], "RESTART")
        initial = json.loads((folder / "initial-guard.json").read_text(encoding="utf-8"))
        self.assertEqual(initial["verdict"], "ALLOW")
        reports = [json.loads(row[0]) for row in lab.store.db.execute("SELECT report FROM guard_reports")]
        codes = {check["code"] for report in reports for check in report["checks"]}
        self.assertIn("RESTART_BUDGET_AVAILABLE", codes)
        self.assertNotIn("RESTART_BUDGET_UNKNOWN", codes)
        self.assertEqual(self.cluster.writes, [])

    def test_attribution_fails_the_run_when_the_chaos_object_is_gone(self):
        lab = self.lab()
        handle(self.incident, lab.store, lab.bus)
        gone = FakeChaosCluster(None)  # Deleted (or expired and garbage-collected) before the outcome was read.
        lab.chaos_cluster = lambda: gone
        with patch("sys.stdout", new=io.StringIO()), \
                self.assertRaisesRegex(RuntimeError, "cannot be attributed to the RESTART: chaos_still_running"):
            lab.recover_incident(self.incident)
        self.assertEqual(lab.summary["attribution"], {"attributed": False, "failed": ["chaos_still_running"]})
        # The repair itself happened and is scored; only the attribution failed.
        self.assertEqual(self.result(lab)["actual"], "RESTART")

    def test_a_forced_action_the_guard_allows_fails_the_run(self):
        from experiments.boutique.harness.chaos_recovery import FORCED
        lab = self.lab()
        with self.assertRaisesRegex(RuntimeError, "was not refused"):
            lab.forced_refusal(self.incident, "RESTART", FORCED[("pod", "ROLLBACK")], "forced-correct-restart")
        # A forced action carries no approval, so a check that passes escalates instead of writing.
        folder = lab.directory / "forced-correct-restart"
        self.assertIsNone(json.loads((folder / "approval.json").read_text(encoding="utf-8")))
        proof = self.proof(lab, "forced-correct-restart")
        self.assertEqual((proof["guard_verdict"], proof["executor_patch_attempts"], proof["template_unchanged"]),
                         ("ESCALATE", 0, True))
        self.assertIn("OPERATOR_APPROVAL_REQUIRED", proof["guard_reason_codes"])
        self.assertEqual((self.cluster.write_attempts, self.cluster.writes), ([], []))

    def test_a_refusal_that_still_wrote_or_changed_the_deployment_fails_the_run(self):
        """The zero-write check stands apart from the verdict: a REJECT with a write attempt, or a changed
        Deployment, fails the run."""
        from experiments.boutique.harness import chaos_recovery
        real = chaos_recovery.execute

        def wrote(proposal, adapters):
            result = real(proposal, adapters)
            self.cluster.write_attempts.append({"action_id": proposal["action_id"]})
            return result

        def changed(proposal, adapters):
            result = real(proposal, adapters)
            self.cluster.snapshot["deployment"]["metadata"]["generation"] += 1
            return result

        for name, fake in (("write attempt", wrote), ("deployment changed", changed)):
            with self.subTest(case=name):
                self.root = Path(tempfile.mkdtemp(dir=self.root))
                lab = self.lab()
                with patch.object(chaos_recovery, "execute", side_effect=fake), \
                        self.assertRaisesRegex(RuntimeError, "attempted a write or changed the Deployment"):
                    lab.forced_refusal(self.incident, "ROLLBACK", chaos_recovery.FORCED[("pod", "ROLLBACK")],
                                       "forced-rollback")
                self.assertEqual(self.proof(lab, "forced-rollback")["guard_verdict"], "REJECT")

    def test_a_reject_with_another_reason_code_fails_the_run(self):
        """A refusal counts for the matrix only with its pre-registered code; any other REJECT fails the run."""
        lab = self.lab()
        # The real Guard refuses this ROLLBACK as ROLLBACK_ALREADY_AT_BASELINE, not as the cell's expected code.
        with self.assertRaisesRegex(RuntimeError, r"Forced ROLLBACK was not refused with RESTART_NOT_JUSTIFIED: "
                                                  r"REJECT \[.*'ROLLBACK_ALREADY_AT_BASELINE'"):
            lab.forced_refusal(self.incident, "ROLLBACK", "RESTART_NOT_JUSTIFIED", "forced-rollback-other-code")
        proof = self.proof(lab, "forced-rollback-other-code")
        self.assertEqual((proof["guard_verdict"], proof["executor_patch_attempts"], proof["expected_reason"]),
                         ("REJECT", 0, "RESTART_NOT_JUSTIFIED"))
        self.assertNotIn("RESTART_NOT_JUSTIFIED", proof["guard_reason_codes"])
        # The CPU cell's RESTART refused only because the budget is spent is not a RESTART_NOT_JUSTIFIED refusal.
        handle(self.incident, lab.store, lab.bus)
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(self.incident)  # The run's own RESTART uses the one-per-hour budget.
        with self.assertRaisesRegex(RuntimeError, "Forced RESTART was not refused with RESTART_NOT_JUSTIFIED"):
            lab.forced_refusal(self.incident, "RESTART", "RESTART_NOT_JUSTIFIED", "forced-restart-budget")
        proof = self.proof(lab, "forced-restart-budget")
        self.assertEqual(proof["guard_verdict"], "REJECT")
        self.assertIn("RESTART_BUDGET_EXHAUSTED", proof["guard_reason_codes"])
        self.assertNotIn("RESTART_NOT_JUSTIFIED", proof["guard_reason_codes"])
        self.assertEqual((proof["executor_patch_attempts"], len(self.cluster.writes)), (0, 1))

    def test_the_drift_restore_rereads_and_retries_a_conflicting_patch(self):
        from experiments.boutique.harness import chaos_recovery
        lab = self.lab()
        self.cluster.snapshot["deployment"]["spec"]["template"]["metadata"].setdefault("labels", {})["drift"] = "1"
        self.patch_conflicts = 1
        pause = Mock()
        with patch.object(chaos_recovery, "time", SimpleNamespace(sleep=pause, monotonic=Mock(return_value=0.0))):
            lab.administrator_restore("drift-restore-patch.json")
        self.assertEqual((lab.summary["drift_cell_restored"], lab.summary["drift_restore_attempts"]), (True, 2))
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"], self.baseline["pod_template"])
        self.assertEqual(len([args for args in self.commands if args[0] == "patch"]), 2)
        pause.assert_called_once_with(1)
        saved = json.loads((lab.directory / "drift-restore-patch.json").read_text(encoding="utf-8"))
        self.assertEqual(saved[:2], [{"op": "test", "path": "/metadata/uid", "value": lab.target["uid"]},
                                     {"op": "test", "path": "/metadata/resourceVersion",
                                      "value": self.cluster.snapshot["deployment"]["metadata"]["resourceVersion"]}])
        # A patch that keeps failing stops after three conditional attempts; nothing is forced.
        self.patch_conflicts = 5
        lab.summary.pop("drift_cell_restored")
        with patch.object(chaos_recovery, "time", SimpleNamespace(sleep=Mock(), monotonic=Mock(return_value=0.0))), \
                self.assertRaisesRegex(RuntimeError, "could not restore the baseline after 3 conditional patches"):
            lab.administrator_restore("drift-restore-patch.json")
        self.assertNotIn("drift_cell_restored", lab.summary)
        self.assertEqual(self.patch_conflicts, 2)


class CpuStressTests(OfflineChaosLab, unittest.TestCase):
    """N3: a metric-only incident is escalated by the rule; a forced RESTART is not justified."""

    scenario_id = "N3"
    fixture = "healthy"

    def test_metric_only_incident_escalates_and_a_forced_restart_writes_nothing(self):
        lab = self.lab(expect="escalated", approval=None)
        alert = {"evidence_id": "E-CPU-1", "source": "otel_metric_alert", "reason": "k8s.pod.cpu.usage",
                 "detail": '{"above":0.18,"metric":"k8s.pod.cpu.usage","unit":"cores","values":[0.19]}',
                 "at": self.incident["observed_at"]}
        live = self.cluster.read(self.incident["target"])["deployment"]["metadata"]
        incident = dict(self.incident, incident_id="INC-CPU-STRESS-1", signals=[alert],
                        observed_resource_version=live["resourceVersion"], observed_generation=live["generation"])
        handle(incident, lab.store, lab.bus)
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(incident)
        self.assertEqual(lab.store.incident(incident["incident_id"])["state"], "NEEDS_REVIEW")
        result = self.result(lab)
        self.assertEqual((result["actual"], result["decision_source"], result["score"], result["executor_writes"]),
                         ("ESCALATE", "rule", "correct", 0))
        forced = self.proof(lab, "forced-restart")
        self.assertIn("RESTART_NOT_JUSTIFIED", forced["guard_reason_codes"])
        self.assertEqual((forced["executor_patch_attempts"], len(self.cluster.writes)), (0, 0))
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])

    def test_without_an_incident_the_cpu_cell_still_runs_before_cleanup(self):
        lab = self.lab(expect="no-incident", approval=None)
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(None)
        forced = self.proof(lab, "forced-restart")
        self.assertEqual((forced["guard_verdict"], forced["executor_patch_attempts"]), ("REJECT", 0))
        self.assertIn("RESTART_NOT_JUSTIFIED", forced["guard_reason_codes"])
        proposal = json.loads((lab.directory / "forced-restart" / "proposal.json").read_text(encoding="utf-8"))
        self.assertTrue(proposal["incident_id"].startswith("INC-FORCED-CPU-"))
        self.assertEqual(self.cluster.writes, [])
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])

    def fault_gate(self, lab):
        """BoutiqueExperiment.fault's no-incident gate, with a detection window that sees nothing."""
        from experiments.boutique.harness import live
        lab.inject_fault, lab.phase, lab.observe = Mock(), Mock(), Mock(return_value=None)
        lab.lab_control, lab.pause_before_injection = None, False
        clock = iter([0.0])
        with patch.object(live, "time", SimpleNamespace(monotonic=lambda: next(clock, 1000.0), sleep=Mock())):
            return live.BoutiqueExperiment.fault(lab, "primary")

    def test_an_acceptable_no_incident_continues_and_the_cpu_cell_still_runs(self):
        """N3's label accepts NO_INCIDENT: with a true-negative proof the run goes on, scored acceptable."""
        lab = self.lab(expect="escalated", approval=None)
        self.observe_window(lab)
        self.assertIsNone(self.fault_gate(lab))
        result = self.result(lab)
        self.assertEqual((result["actual"], result["score"], result["matched_expected"], result["executor_writes"]),
                         ("NO_INCIDENT", "acceptable", False, 0))
        self.assertTrue(result["true_negative"]["passed"])
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(None)
        forced = self.proof(lab, "forced-restart")
        self.assertEqual((forced["guard_verdict"], forced["executor_patch_attempts"]), ("REJECT", 0))
        self.assertIn("RESTART_NOT_JUSTIFIED", forced["guard_reason_codes"])
        self.assertEqual(self.cluster.writes, [])
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])
        # The PASS line names the decision that was scored, not only the outcome the run expected.
        from experiments.boutique.harness.live import chaos_pass_line
        self.assertEqual(chaos_pass_line("N3", "escalated", lab.summary),
                         "PASS: Chaos Mesh scenario N3 ended in NO_INCIDENT, which its label scores acceptable "
                         "(run expected escalated); 1 forced wrong action(s) refused with zero writes; "
                         "chaos cleanup confirmed")

    def test_an_acceptable_no_incident_without_a_true_negative_proof_fails_and_says_so(self):
        lab = self.lab(expect="escalated", approval=None)
        self.observe_window(lab, complete=False)  # Blind: no reading had complete metric thresholds.
        with self.assertRaisesRegex(RuntimeError, "No incident opened in the detection window: scenario N3 scored "
                                                  "NO_INCIDENT acceptable; true-negative proof failed"):
            self.fault_gate(lab)
        self.assertFalse(self.result(lab)["true_negative"]["passed"])


class CpuStressUnreadyPodTests(OfflineChaosLab, unittest.TestCase):
    """N3 when the stressed Pod also fails readiness: RESTART_NOT_JUSTIFIED does not fire, and the Guard's other
    checks pass, so only the missing approval stands between the forced RESTART and a write."""

    scenario_id = "N3"
    fixture = "pod-failure"

    def test_a_forced_restart_the_guard_would_allow_escalates_with_zero_writes(self):
        lab = self.lab(expect="escalated", approval=None)
        with self.assertRaisesRegex(RuntimeError, "Forced RESTART was not refused with RESTART_NOT_JUSTIFIED"):
            lab.forced_refusal(self.incident, "RESTART", "RESTART_NOT_JUSTIFIED", "forced-restart")
        proof = self.proof(lab, "forced-restart")
        self.assertEqual((proof["guard_verdict"], proof["guard_reason_codes"]),
                         ("ESCALATE", ["OPERATOR_APPROVAL_REQUIRED"]))
        self.assertEqual((proof["executor_patch_attempts"], proof["template_unchanged"],
                          proof["generation_unchanged"]), (0, True, True))
        self.assertEqual((self.cluster.write_attempts, self.cluster.writes), ([], []))


class NetworkControlTests(OfflineChaosLab, unittest.TestCase):
    """T1: no incident is scored correct only with a true-negative proof, never by omission."""

    scenario_id = "T1"
    fixture = "healthy"

    def observe(self, lab, *, complete=True, count=4):
        self.observe_window(lab, complete=complete, count=count)

    def test_no_incident_with_proof_is_correct_and_continues(self):
        lab = self.lab(expect="no-incident", approval=None)
        self.observe(lab)
        self.assertTrue(lab.no_incident("primary"))
        result = self.result(lab)
        self.assertEqual((result["actual"], result["score"], result["executor_writes"]), ("NO_INCIDENT", "correct", 0))
        self.assertTrue(result["true_negative"]["passed"])
        with patch("sys.stdout", new=io.StringIO()):
            lab.recover_incident(None)
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])
        self.assertEqual(self.cluster.writes, [])
        self.assertFalse((lab.directory / "forced-restart").exists())  # The CPU cell belongs to N3 only.
        from experiments.boutique.harness.live import chaos_pass_line
        self.assertEqual(chaos_pass_line("T1", "no-incident", lab.summary),
                         "PASS: Chaos Mesh scenario T1 reached its expected outcome (no-incident) with NO_INCIDENT; "
                         "chaos cleanup confirmed")

    def test_no_incident_without_evidence_does_not_pass(self):
        for name, change in (("blind", lambda lab: self.observe(lab, complete=False)),
                             ("not injected", lambda lab: (self.observe(lab), setattr(lab, "chaos_record", None))),
                             ("short window", lambda lab: self.observe(lab, count=1))):
            with self.subTest(case=name):
                self.root = Path(tempfile.mkdtemp(dir=self.root))
                lab = self.lab(expect="no-incident", approval=None)
                change(lab)
                self.assertFalse(lab.no_incident("primary"))
                self.assertFalse(self.result(lab)["true_negative"]["passed"])

    def test_an_executor_action_breaks_the_control(self):
        lab = self.lab(expect="no-incident", approval=None)
        self.observe(lab)
        # Even a refused action reached the Executor ledger, so "nothing happened" is no longer true.
        with patch("sys.stdout", new=io.StringIO()):
            lab.forced_refusal(self.incident, "RESTART", "RESTART_NOT_JUSTIFIED", "forced-restart")
        self.assertFalse(lab.no_incident("primary"))
        self.assertEqual(self.result(lab)["true_negative"]["checks"]["no_executor_action"], False)


class FakeInjectionCluster:
    """chaos.Cluster as inject_fault, cleanup and attribution use it, for the connected namespace.

    records are the containerRecords ids Chaos Mesh reports; pods maps each Pod name to (uid, app label).
    The object reports injection on the confirm_after-th read (never when None).
    """

    def __init__(self, records, pods, *, confirm_after=1, fail_dry_run=False, fail_create=False):
        self.records, self.pods, self.confirm_after = list(records), dict(pods), confirm_after
        self.fail_dry_run, self.fail_create = fail_dry_run, fail_create
        self.calls, self.removed, self.reads, self.document = [], [], 0, None

    def create(self, document, *, dry_run=False):
        self.calls.append(("create", "dry-run" if dry_run else "real"))
        if dry_run and self.fail_dry_run:
            raise RuntimeError("kubectl create failed: admission webhook denied the request")
        if not dry_run:
            self.document = deepcopy(document)
            self.document["metadata"]["uid"] = "chaos-object-uid"
            if self.fail_create:
                raise RuntimeError("kubectl create failed: connection reset after the object was stored")
        return deepcopy(document)

    def current(self):
        document = deepcopy(self.document)
        injected = self.confirm_after is not None and self.reads >= self.confirm_after
        document["status"] = {
            "conditions": [{"type": "Selected", "status": "True"},
                           {"type": "AllInjected", "status": "True" if injected else "False"}],
            "experiment": {"desiredPhase": "Run", "containerRecords": [
                {"id": record, "phase": "Injected" if injected else "Not Injected"} for record in self.records]}}
        return document

    def get(self, kind, name):
        self.calls.append(("get", kind, name))
        if kind == "pod":
            uid, app = self.pods[name]
            return {"metadata": {"name": name, "uid": uid, "labels": {"app": app}}}
        self.reads += 1
        return self.current()

    def call(self, *args, **kwargs):
        self.calls.append(("call",) + args)
        if args[0] != "get":
            raise AssertionError("unexpected chaos command " + str(args))
        return json.dumps(self.current()) if self.document else ""

    def remove(self, kind, name):
        self.removed.append((kind, name))
        self.document = None


class InjectFaultTests(OfflineChaosLab, unittest.TestCase):
    """inject_fault(): dry run first, confirmed injection, the selected Pods' UIDs, and refusals that stop the run."""

    POD = "shippingservice-6d4f9c7b8-abcde"

    def lab(self, **options):
        lab = super().lab(**options)
        lab.chaos_record = lab.chaos_document = None
        lab.cluster_id, lab.context, lab.namespace, lab.name = (
            "sentinel-boutique-connected", CONNECTED_CONTEXT, CONNECTED_NAMESPACE, "shippingservice")
        return lab

    def inject(self, fake, lab=None):
        from experiments.boutique.harness import chaos_recovery
        lab = lab or self.lab()
        lab.chaos_cluster = lambda: fake
        clock = iter(range(0, 10_000, 25))
        with patch.object(chaos_recovery, "time", SimpleNamespace(monotonic=lambda: float(next(clock)), sleep=Mock())):
            lab.inject_fault()
        return lab

    def records(self, *names):
        return [CONNECTED_NAMESPACE + "/" + name + "/server" for name in names]

    def test_a_confirmed_injection_saves_the_selected_pod_uids_and_attribution_uses_them(self):
        from experiments.boutique.harness.chaos_recovery import OWNER, owner_label
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid-selected", "shippingservice")},
                                    confirm_after=2)
        lab = self.inject(fake)
        self.assertEqual(fake.calls[:2], [("create", "dry-run"), ("create", "real")])
        self.assertEqual(fake.reads, 2)  # Not confirmed on the first read; the harness polled again.
        request = json.loads((lab.directory / "chaos-request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["metadata"]["labels"][OWNER], owner_label(lab.directory.name))
        self.assertEqual(request["spec"]["selector"], {"namespaces": [CONNECTED_NAMESPACE],
                                                       "labelSelectors": {"app": "shippingservice"}})
        saved = json.loads((lab.directory / "chaos-experiment.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["selected_pods"], [{"name": self.POD, "uid": "uid-selected"}])
        self.assertEqual((saved["uid"], saved["duration_seconds"], saved["record_ids"]),
                         ("chaos-object-uid", 420, self.records(self.POD)))
        created = datetime.fromisoformat(saved["created_at"].replace("Z", "+00:00"))
        self.assertEqual(saved["expires_at"].replace("Z", "+00:00"),
                         (created + timedelta(seconds=420)).isoformat().replace(".000000", ""))
        self.assertEqual(lab.summary["chaos"]["name"], saved["name"])
        self.assertTrue(saved["name"].startswith("sentinel-pod-"))
        # The saved record decides attribution: the selected UID still running fails it, even under a new name.
        folder = lab.directory / "chaos-recovery"
        folder.mkdir()
        (folder / "outcome.json").write_text(json.dumps({"status": "RECOVERED", "window_end": saved["created_at"]}),
                                             encoding="utf-8")
        self.listed_pods = ({"name": "shippingservice-renamed", "uid": "uid-selected"},)
        with self.assertRaisesRegex(RuntimeError, "cannot be attributed to the RESTART: new_pods_not_selected$"):
            lab.attribute(folder)
        proof = json.loads((lab.directory / "attribution.json").read_text(encoding="utf-8"))
        self.assertEqual(proof["checks"][1]["detail"]["selected_uids_still_running"], ["uid-selected"])
        self.listed_pods = ({"name": "shippingservice-new", "uid": "uid-fresh"},)
        with patch("sys.stdout", new=io.StringIO()):
            lab.attribute(folder)
        self.assertEqual(lab.summary["attribution"], {"attributed": True, "failed": []})

    def test_an_injection_not_confirmed_in_time_stops_the_run_and_cleanup_still_removes_it(self):
        from experiments.boutique.harness.chaos_recovery import INJECTION_CONFIRM_SECONDS
        self.assertEqual(INJECTION_CONFIRM_SECONDS, 60)
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid-selected", "shippingservice")},
                                    confirm_after=None)
        lab = self.lab()
        with self.assertRaisesRegex(RuntimeError, "did not confirm injection"):
            self.inject(fake, lab)
        self.assertEqual(fake.reads, 3)  # Reads at 25 s and 50 s pass the 60 s deadline check; 75 s does not.
        self.assertIsNone(lab.chaos_record)
        self.assertFalse((lab.directory / "chaos-experiment.json").exists())
        lab.remove_chaos()
        self.assertEqual(fake.removed, [("PodChaos", lab.chaos_document["metadata"]["name"])])

    def test_a_selected_pod_of_another_app_is_refused(self):
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid-frontend", "frontend")})
        lab = self.lab()
        with self.assertRaisesRegex(RuntimeError, "selected a Pod that is not the target's"):
            self.inject(fake, lab)
        self.assertIsNone(lab.chaos_record)
        self.assertFalse((lab.directory / "chaos-experiment.json").exists())

    def test_records_it_cannot_parse_are_refused(self):
        for records in (["shippingservice-abc"], ["other-namespace/" + self.POD], [CONNECTED_NAMESPACE + "/Bad_Pod"]):
            with self.subTest(records=records):
                self.root = Path(tempfile.mkdtemp(dir=self.root))
                fake = FakeInjectionCluster(records, {self.POD: ("uid-selected", "shippingservice")})
                lab = self.lab()
                with self.assertRaisesRegex(ValueError, "Unexpected Chaos Mesh record id"):
                    self.inject(fake, lab)
                self.assertFalse((lab.directory / "chaos-experiment.json").exists())
                self.assertFalse(any(call[:2] == ("get", "pod") for call in fake.calls))

    def test_a_failed_dry_run_creates_nothing_and_a_failed_create_is_still_cleaned_up(self):
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid", "shippingservice")}, fail_dry_run=True)
        lab = self.lab()
        with self.assertRaisesRegex(RuntimeError, "admission webhook denied"):
            self.inject(fake, lab)
        self.assertEqual(fake.calls, [("create", "dry-run")])
        self.assertIsNone(lab.chaos_document)
        # The API server stored the object but the call failed: the run still knows it and removes it.
        self.root = Path(tempfile.mkdtemp(dir=self.root))
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid", "shippingservice")}, fail_create=True)
        lab = self.lab()
        with self.assertRaisesRegex(RuntimeError, "connection reset"):
            self.inject(fake, lab)
        lab.emergency_restore()
        self.assertEqual(fake.removed, [("PodChaos", lab.chaos_document["metadata"]["name"])])
        self.assertTrue(lab.summary["chaos_cleanup_confirmed"])

    def test_the_wrong_scope_or_workload_is_refused_before_any_chaos_command(self):
        from experiments.boutique.harness.scenarios import CatalogError
        fake = FakeInjectionCluster(self.records(self.POD), {self.POD: ("uid", "shippingservice")})
        lab = self.lab()
        lab.name = "cartservice"
        with self.assertRaisesRegex(RuntimeError, "targets another workload"):
            self.inject(fake, lab)
        self.root = Path(tempfile.mkdtemp(dir=self.root))
        lab = self.lab()
        lab.context = "kind-sentinel-boutique-lab"
        with self.assertRaises(CatalogError):
            self.inject(fake, lab)
        self.assertEqual(fake.calls, [])


class EmergencyRestoreTests(OfflineChaosLab, unittest.TestCase):
    """emergency_restore(): remove the run's chaos object, then restore the template, even when removal fails."""

    def test_removes_the_chaos_object_then_restores_the_baseline(self):
        lab = self.lab()
        self.cluster.snapshot["deployment"]["spec"]["template"]["metadata"].setdefault("labels", {})["drift"] = "1"
        self.assertTrue(lab.emergency_restore())
        self.assertEqual(self.chaos.removed, [("PodChaos", "sentinel-pod-test")])
        self.assertEqual((lab.summary["chaos_cleanup_confirmed"], lab.summary["operator_emergency_restore"]),
                         (True, True))
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"], self.baseline["pod_template"])

    def test_a_failed_removal_is_logged_and_the_restore_still_runs(self):
        from experiments.boutique.harness import chaos_recovery
        lab = self.lab()
        self.chaos.remove = Mock(side_effect=RuntimeError("Cleanup not confirmed for PodChaos/sentinel-pod-test"))
        with patch.object(chaos_recovery.log, "failure") as failure:
            self.assertTrue(lab.emergency_restore())
        self.assertEqual(failure.call_args.args[1], "boutique.chaos_cleanup.failed")
        self.assertEqual((lab.summary["chaos_cleanup_confirmed"], lab.summary["operator_emergency_restore"]),
                         (False, True))
        self.assertEqual(self.cluster.snapshot["deployment"]["spec"]["template"], self.baseline["pod_template"])

    def test_another_runs_chaos_object_is_never_removed(self):
        lab = self.lab()
        self.chaos.document["metadata"]["labels"][OWNER] = "another-run"
        self.assertTrue(lab.emergency_restore())
        self.assertEqual(self.chaos.removed, [])
        self.assertFalse(lab.summary["chaos_cleanup_confirmed"])


class ChaosFaultImpactTests(OfflineChaosLab, unittest.TestCase):
    """fault(): the pod failure keeps the catalog's storefront check; CPU and network record impact only."""

    scenario_id = "N3"
    fixture = "healthy"

    def test_cpu_and_network_record_the_storefront_impact_without_requiring_it(self):
        from experiments.boutique.harness import chaos_recovery
        from experiments.recovery import live as recovery
        for scenario_id in ("N3", "T1"):
            with self.subTest(scenario=scenario_id):
                self.root = Path(tempfile.mkdtemp(dir=self.root))
                self.item = chaos_recovery.scenarios.scenario(scenario_id)
                lab = self.lab(approval=None)
                journey = {"passed": False, "steps": [{"name": "checkout", "status": 500}]}
                lab.storefront, lab.record_shopping = Mock(journey=Mock(return_value=journey)), Mock()
                incident = {"incident_id": "INC-CPU-1"} if scenario_id == "N3" else None
                with patch.object(chaos_recovery.BoutiqueExperiment, "fault", return_value=incident) as detect, \
                        patch.object(recovery.Experiment, "fault", side_effect=AssertionError("pod path")):
                    self.assertEqual(lab.fault("primary"), incident)
                detect.assert_called_once_with(lab, "primary")
                saved = json.loads((lab.directory / "primary-storefront-impact.json").read_text(encoding="utf-8"))
                self.assertEqual(saved, journey)  # A failing journey is recorded, never a reason to stop.
                lab.record_shopping.assert_called_once_with("primary-fault", journey,
                                                            incident_id=(incident or {}).get("incident_id"))

    def test_the_pod_failure_keeps_the_predicted_storefront_check(self):
        from experiments.boutique.harness import chaos_recovery
        from experiments.recovery import live as recovery
        self.item = chaos_recovery.scenarios.scenario("N2")
        lab = self.lab(approval=None)
        lab.storefront = Mock(journey=Mock(side_effect=AssertionError("impact is checked by the base fault")))
        with patch.object(recovery.Experiment, "fault", return_value=self.incident) as base, \
                patch.object(chaos_recovery.BoutiqueExperiment, "fault", side_effect=AssertionError("cpu path")):
            self.assertEqual(lab.fault("primary"), self.incident)
        base.assert_called_once_with("primary")
        self.assertFalse((lab.directory / "primary-storefront-impact.json").exists())


class HarnessWiringTests(unittest.TestCase):
    """prepare(), prepare_image(), wrap_observer() and observe() with fakes for the cluster parts: the Guard
    policy the harness writes, and the metrics-server bridge feeding the scenario's own thresholds."""

    def setUp(self):
        from experiments.boutique.harness import chaos_recovery
        from experiments.recovery import live as recovery
        from tests.test_observer_telemetry import observation
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root, self.steps, self.usage = Path(temp.name), [], {}
        for patcher in (patch("subprocess.run", side_effect=AssertionError("cluster command attempted")),
                        patch("shared.provenance.collect", return_value={"source_revision": "test"})):
            patcher.start()
            self.addCleanup(patcher.stop)
        baseline = json.loads((ROOT / "data/sentinel/samples/baseline.json").read_text(encoding="utf-8"))
        test = self
        self.at = datetime.fromisoformat(recovery.utc().replace("Z", "+00:00"))

        class Base(recovery.Experiment):
            """The connected experiment's cluster parts, faked; Experiment.prepare itself is real."""

            def deployment(self):
                return {"metadata": {"name": self.name, "namespace": self.namespace, "uid": "deployment-uid",
                                     "resourceVersion": "7", "generation": 3},
                        "spec": {"template": deepcopy(baseline["pod_template"])},
                        "status": {"readyReplicas": 1, "availableReplicas": 1}}

            def health_window(self, expected_template):
                return [{"at": recovery.utc()} for _ in range(3)]

            def prepare_image(self):
                test.steps.append("prepare_image")
                self.observer_collector = SimpleNamespace(target={"name": self.name},
                                                          collect=lambda: observation(test.at))
                return "image@sha256:" + "a" * 64

            def observe(self, label):
                test.steps.append("observe")
                return test.observer.observe(self.observer_collector.collect())

            def kubectl(self, *args, **kwargs):
                stdout = ""
                if args[:2] == ("config", "view"):
                    stdout = json.dumps({"clusters": [{"cluster": {"server": "https://127.0.0.1:6443",
                                                                    "certificate-authority-data": "Q0E="}}]})
                elif args[:2] == ("create", "token"):
                    stdout = "token-not-used"
                elif args[0] != "apply":
                    raise AssertionError("unexpected kubectl " + str(args))
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        self.lab_type = type("WiredChaosExperiment", (chaos_recovery.ChaosRecoveryMixin, Base), {
            "scenario": property(lambda lab: test.item), "event": lambda lab, *args, **kwargs: None})

        class Reader:
            def get(self, path):
                test.steps.append(path)
                return {"items": [{"metadata": {"name": name, "namespace": "sentinel-demo"},
                                   "timestamp": test.at.isoformat().replace("+00:00", "Z"), "window": "15s",
                                   "containers": [{"name": container, "usage": {"cpu": cpu, "memory": "32Mi"}}]}
                                  for name, (container, cpu) in test.usage.items()]}

        self.reader = Reader()

    def lab(self, scenario_id="N3", **scope):
        from experiments.boutique.harness import scenarios
        self.item = scenarios.scenario(scenario_id)
        lab = self.lab_type(self.root / ("run-" + str(len(list(self.root.iterdir())))))
        self.addCleanup(lab.store.close)
        lab.cluster_id, lab.context, lab.namespace, lab.name = (
            scope.get("cluster_id", "sentinel-boutique-connected"), scope.get("context", CONNECTED_CONTEXT),
            CONNECTED_NAMESPACE, "shippingservice")
        return lab

    def prepare(self, lab):
        from experiments.boutique.harness import chaos_recovery

        def preflight(cluster):
            self.steps.append("preflight")
            self.assertEqual((cluster.context, cluster.namespace), (CONNECTED_CONTEXT, CONNECTED_NAMESPACE))
            return {"pod_metrics_items": 1}
        with patch.object(chaos_recovery, "preflight", side_effect=preflight), \
                patch.object(chaos_recovery, "KubectlReader", return_value=self.reader) as reader, \
                patch("sys.stdout", new=io.StringIO()):
            lab.prepare()
        reader.assert_called_once_with(CONNECTED_CONTEXT)
        return lab

    def test_prepare_writes_a_policy_that_allows_a_budgeted_restart_and_the_guard_accepts_it(self):
        from services.guard.core import _validate_policy
        from services.guard.common import GuardInputError
        from services.guard.decisions import validate_policy
        lab = self.prepare(self.lab())
        self.assertEqual(self.steps[:2], ["preflight", "prepare_image"])  # Read-only checks before any setup.
        policy = json.loads((lab.directory / "policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy, lab.policy)
        self.assertEqual(policy["allowed_actions"], ["ROLLBACK", "RESTART"])
        self.assertEqual(policy["restart_constraints"], {"targets": [lab.target], "max_restarts": 1,
                                                         "window_seconds": 3600})
        self.assertEqual(lab.target["uid"], "deployment-uid")
        self.assertEqual((policy["profile"], policy["require_operator_approval"]), ("live", True))
        _validate_policy(policy)
        validate_policy(policy, require_live=True)
        self.assertEqual(lab.summary["chaos_preflight"], {"pod_metrics_items": 1})
        # Without the harness's restart constraints, the live Guard refuses the policy outright.
        policy.pop("restart_constraints")
        with self.assertRaisesRegex(GuardInputError, "requires restart_constraints"):
            validate_policy(policy, require_live=True)

    def test_prepare_refuses_another_scope_before_the_preflight(self):
        from experiments.boutique.harness.scenarios import CatalogError
        for scope in ({"context": "kind-sentinel-boutique-lab", "cluster_id": "sentinel-boutique-lab"},
                      {"cluster_id": "sentinel-lab"}):
            with self.subTest(scope=scope), self.assertRaises(CatalogError):
                self.prepare(self.lab(**scope))
        self.assertEqual(self.steps, [])

    def test_the_observer_alerts_through_the_bridge_with_the_scenarios_thresholds(self):
        from services.observer.core import Observer
        from services.observer.telemetry import CorrelatedEvidenceCollector
        from tests.test_observer_telemetry import POLICY, observation
        lab = self.prepare(self.lab())
        self.assertIsInstance(lab.observer_collector, CorrelatedEvidenceCollector)
        self.assertEqual(lab.observer_collector.policy["metric_thresholds"], self.item["observer_thresholds"])
        self.assertEqual(lab.observer_collector.policy["metric_thresholds"],
                         [{"name": "k8s.pod.cpu.usage", "unit": "cores", "above": 0.18}])
        pod = observation()["pods"][0]
        container = pod["status"]["containerStatuses"][0]["name"]
        self.observer, incident = Observer(POLICY), None
        # 190 millicores is above the 0.18-core threshold on every reading for 15 s: a metric alert incident.
        self.usage = {pod["metadata"]["name"]: (container, "190m")}
        start = self.at
        for offset in (0, 15):
            self.at = start + timedelta(seconds=offset)
            lab.metrics_bridge.now = lambda at=self.at: at
            incident = lab.observe("primary-" + str(offset)) or incident
        self.assertIn("/apis/metrics.k8s.io/v1beta1/namespaces/sentinel-demo/pods", self.steps)
        self.assertIsNotNone(incident)
        self.assertIn("otel_metric_alert", {signal["source"] for signal in incident["signals"]})
        reports = json.loads((lab.directory / "metrics-bridge.json").read_text(encoding="utf-8"))
        self.assertEqual([report["complete"] for report in reports], [True, True])

    def test_a_pod_without_metrics_leaves_the_thresholds_incomplete_and_raises_nothing(self):
        lab = self.prepare(self.lab("T1"))
        self.usage = {}
        telemetry = lab.observer_collector.collect()["telemetry"]
        self.assertFalse(telemetry["thresholds_complete"])
        self.assertEqual(telemetry["alerts"], [])
        self.assertEqual(lab.metrics_bridge.reports[-1]["complete"], False)
        # At or below the threshold with full coverage: complete, and still no alert.
        from tests.test_observer_telemetry import observation
        pod = observation()["pods"][0]
        self.usage = {pod["metadata"]["name"]: (pod["status"]["containerStatuses"][0]["name"], "180m")}
        telemetry = lab.observer_collector.collect()["telemetry"]
        self.assertTrue(telemetry["thresholds_complete"])
        self.assertEqual(telemetry["alerts"], [])


if __name__ == "__main__":
    unittest.main()
