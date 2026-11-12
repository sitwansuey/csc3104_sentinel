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

    def kubectl(self, *args, **kwargs):
        """The administrator's kubectl: Pod listing, the R1 drift injection, the restore patch, rollout status."""
        self.commands.append(args)
        template = self.cluster.snapshot["deployment"]["spec"]["template"]
        stdout = ""
        if args[:2] == ("get", "pods"):
            stdout = json.dumps({"items": [{"metadata": {"name": "demo-api-new", "uid": "new-pod-uid"}}]})
        elif args[:2] == ("set", "env"):
            name, value = args[-1].split("=", 1)
            template["spec"]["containers"][0]["env"].append({"name": name, "value": value})
        elif args[0] == "patch":
            patch_document = json.loads(Path(args[args.index("--patch-file") + 1]).read_text(encoding="utf-8"))
            self.cluster.snapshot["deployment"]["spec"]["template"] = deepcopy(patch_document[-1]["value"])
        elif args[0] != "rollout":
            raise AssertionError("unexpected kubectl " + str(args))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

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
        from experiments.recovery.live import save, utc
        start = self.real_now() - timedelta(seconds=130)
        for index in range(count):
            save(lab.directory / f"primary-{index}-observation.json",
                 {"captured_at": utc(start + timedelta(seconds=index * 130 / max(count - 1, 1))),
                  "telemetry": {"thresholds_complete": complete}})

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


if __name__ == "__main__":
    unittest.main()
