"""Offline connected-workload checks; HTTP services and Kubernetes are fake."""

from contextlib import redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from experiments.boutique.harness.connected import (APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, ConnectedBoutiqueExperiment,
                                        configured_graph, connected_manifest, decoded_resources, image_digest, readiness)
from experiments.boutique.harness import targets
from experiments.boutique.harness.live import shipping_manifest, validate_cart
from experiments.boutique.harness.scenarios import CatalogError
from experiments.boutique.traffic.storefront import StorefrontProbe
from scripts.test_boutique_live import BoutiqueExperiment
from services.executor.store import fingerprint
from services.verifier.core import verify

ROOT = Path(__file__).resolve().parents[1]

def resources():
    items = []
    for name in sorted(APPLICATIONS):
        items.append({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "generation": 1},
            "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [
                    {"name": "server", "image": name + ":1", "env": [{"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"}]}]}}},
            "status": {"observedGeneration": 1, "readyReplicas": 1, "availableReplicas": 1}})
        items.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": {"type": "ClusterIP"}})
    items += [{"kind": "Deployment", "metadata": {"name": "loadgenerator"}},
              {"kind": "Service", "metadata": {"name": "frontend-external"}, "spec": {"type": "LoadBalancer"}}]
    return items


class ConnectedConfigTests(unittest.TestCase):
    def test_kubectl_multi_document_json_and_lists_are_decoded_without_yaml_dependencies(self):
        first = {"kind": "Deployment", "metadata": {"name": "one"}}
        second = {"kind": "Service", "metadata": {"name": "two"}}
        self.assertEqual(decoded_resources(json.dumps(first) + "\n" + json.dumps(second)), [first, second])
        self.assertEqual(decoded_resources(json.dumps({"kind": "List", "items": [first, second]})), [first, second])
        with self.assertRaises(ValueError):
            decoded_resources(json.dumps(first) + "trailing invalid text")
    def test_pinned_upstream_source_and_immutable_repository_selection(self):
        self.assertEqual(hashlib.sha256(UPSTREAM.read_bytes()).hexdigest(), UPSTREAM_SHA256)
        self.assertEqual(image_digest("redis:alpine", [{"RepoDigests": ["redis@sha256:" + "a" * 64]}]),
                         "docker.io/library/redis@sha256:" + "a" * 64)
        for values in ([], [{"RepoDigests": ["different@sha256:" + "a" * 64]}],
                       [{"RepoDigests": ["redis@sha256:" + "a" * 64, "redis@sha256:" + "b" * 64]}]):
            with self.assertRaises(RuntimeError):
                image_digest("redis:alpine", values)

    def test_manifest_retains_full_app_wiring_but_not_public_service_or_loadgen(self):
        original = resources()
        saved = deepcopy(original)
        images = {name + ":1": "example/" + name + "@sha256:" + "a" * 64 for name in APPLICATIONS}
        result = connected_manifest(original, images, "sentinel-boutique-connected")
        self.assertEqual(original, saved)
        deployments = [item for item in result["items"] if item["kind"] == "Deployment"]
        self.assertEqual({item["metadata"]["name"] for item in deployments}, APPLICATIONS)
        self.assertFalse(any(item["metadata"]["name"] in {"loadgenerator", "frontend-external"} for item in result["items"]))
        for deployment in deployments:
            pod = deployment["spec"]["template"]["spec"]
            self.assertFalse(pod["automountServiceAccountToken"])
            self.assertEqual(pod["containers"][0]["imagePullPolicy"], "Never")
        graph = configured_graph(result)
        self.assertIn({"from": "frontend", "to": "shippingservice"}, graph["edges"])
        self.assertEqual(set(graph["nodes"]), APPLICATIONS)

    def test_incomplete_or_unreviewed_deployments_are_rejected(self):
        images = {name + ":1": "example@sha256:" + "a" * 64 for name in APPLICATIONS}
        for items in (resources()[2:], resources() + [{"kind": "ClusterRole", "metadata": {"name": "admin"}}]):
            with self.assertRaises((ValueError, RuntimeError)):
                connected_manifest(items, images, "sentinel-boutique-connected")

    def test_dependency_readiness_does_not_fabricate_missing_or_stale_health(self):
        deployments = [item for item in resources() if item.get("kind") == "Deployment"
                       and item["metadata"]["name"] in APPLICATIONS]
        self.assertTrue(all(readiness(deployments).values()))
        frontend = next(item for item in deployments if item["metadata"]["name"] == "frontend")
        frontend["status"]["observedGeneration"] = 0
        self.assertFalse(readiness(deployments)["frontend"])
        frontend["status"]["readyReplicas"] = True
        self.assertIsNone(readiness(deployments)["frontend"])
        self.assertIsNone(readiness([])["checkoutservice"])

    def test_recovery_scope_stays_on_shipping_and_isolated_from_old_lab(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = ConnectedBoutiqueExperiment(Path(directory) / "run")
            try:
                self.assertEqual(experiment.name, "shippingservice")
                self.assertEqual(experiment.cluster_id, "sentinel-boutique-connected")
                self.assertEqual(experiment.namespace, "sentinel-boutique-connected")
                self.assertEqual(experiment.storefront_url, "http://127.0.0.1:8080")
            finally:
                experiment.store.close()

    def test_failed_or_missing_checkout_evidence_cannot_establish_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = ConnectedBoutiqueExperiment(Path(directory) / "run")
            try:
                deployments = [item for item in resources() if item.get("kind") == "Deployment"
                               and item["metadata"]["name"] in APPLICATIONS]
                target = next(item for item in deployments if item["metadata"]["name"] == "shippingservice")
                target["metadata"]["uid"] = "shipping-uid"
                identity = {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace,
                            "kind": "Deployment", "name": experiment.name, "uid": "shipping-uid"}
                digest = fingerprint(target["spec"]["template"])
                execution = {"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-1",
                    "action_id": "ACT-1", "target": identity, "status": "APPLIED", "resource_version": "2",
                    "recorded_at": (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                    "details": "Offline test execution"}
                sample = {"target": identity, "service": experiment.name, "protocol": "grpc", "grpc_status": 0,
                    "application_check_passed": True, "ready_replicas": 1, "available_replicas": 1,
                    "desired_replicas": 1, "template_sha256": digest, "configuration_stable": True}
                policy = {"health": {"protocol": "grpc", "required_grpc_status": 0, "minimum_samples": 3,
                    "stable_window_seconds": 1, "max_sample_gap_seconds": 90,
                    "required_dependencies": ["storefront-checkout"], "require_template_match": True,
                    "expected_template_sha256": digest, "require_target_identity": True}}
                experiment.kubectl = Mock(return_value=SimpleNamespace(stdout=json.dumps({"items": deployments})))
                for application_result, expected in ((False, "NOT_RECOVERED"), (None, "UNKNOWN")):
                    experiment.storefront = Mock()
                    experiment.storefront.journey.return_value = {"passed": application_result}
                    with patch.object(BoutiqueExperiment, "verification_samples",
                                      return_value=iter([deepcopy(sample) for _ in range(3)])):
                        measured = list(experiment.verification_samples(execution))
                    self.assertIs(measured[0]["dependencies"]["storefront-checkout"], application_result)
                    self.assertEqual(verify(execution, measured, policy)["status"], expected)
                # A supporting request can succeed while the Deployment changes.
                # Final state must replace the pre-request replica observation.
                experiment.storefront.journey.return_value = {"passed": True}
                target["spec"]["replicas"] = 2
                experiment.kubectl.return_value.stdout = json.dumps({"items": deployments})
                with patch.object(BoutiqueExperiment, "verification_samples",
                                  return_value=iter([deepcopy(sample) for _ in range(3)])):
                    measured = list(experiment.verification_samples(execution))
                self.assertEqual(measured[0]["desired_replicas"], 2)
                self.assertEqual(verify(execution, measured, policy)["status"], "NOT_RECOVERED")
            finally:
                experiment.store.close()


def connected_lab(directory, target="shippingservice"):
    experiment = ConnectedBoutiqueExperiment(Path(directory) / "run")
    experiment.select_target(target)
    return experiment


SHIPPING_FAULT = {"home_status": 200, "add_to_cart_status": 302, "cart_status": 500, "checkout_status": 500,
                  "passed": False}
CART_FAULT = {"home_status": 500, "add_to_cart_status": 500, "cart_status": 500, "checkout_status": 500,
              "passed": False}


class CartTargetTests(unittest.TestCase):
    """The second catalog target (P3.09): overrides, readiness, RBAC, probe and impact come from the catalog."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        for patcher in (patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lab(self, target="shippingservice"):
        experiment = connected_lab(self.directory + "/" + target, target)
        self.addCleanup(experiment.store.close)
        return experiment

    def test_manifest_applies_the_cart_reviewed_overrides_and_keeps_shipping_reviewed_spec(self):
        images = {name + ":1": "example/" + name + "@sha256:" + "a" * 64 for name in APPLICATIONS}
        original = resources()
        result = connected_manifest(original, images, "sentinel-boutique-connected")
        deployments = {item["metadata"]["name"]: item for item in result["items"] if item["kind"] == "Deployment"}
        upstream = next(item for item in original if item["kind"] == "Deployment"
                        and item["metadata"]["name"] == "cartservice")
        cart = deployments["cartservice"]["spec"]
        self.assertEqual(cart["strategy"], {"type": "Recreate"})
        self.assertEqual(cart["minReadySeconds"], 2)
        # Only the reviewed overrides change; the upstream wiring and container stay.
        self.assertEqual(set(cart), set(upstream["spec"]) | {"strategy", "minReadySeconds"})
        self.assertEqual((cart["replicas"], cart["selector"]), (upstream["spec"]["replicas"], upstream["spec"]["selector"]))
        self.assertEqual(cart["template"]["spec"]["containers"][0]["env"],
                         upstream["spec"]["template"]["spec"]["containers"][0]["env"])
        reviewed = shipping_manifest("example/shippingservice@sha256:" + "a" * 64)["items"][2]["spec"]
        self.assertEqual(deployments["shippingservice"]["spec"], reviewed)
        self.assertEqual({key: reviewed[key] for key in ("strategy", "minReadySeconds")},
                         targets.profile("shippingservice").spec_overrides)
        # Non-target applications keep the upstream rollout strategy.
        self.assertNotIn("strategy", deployments["frontend"]["spec"])

    def test_readiness_excludes_the_selected_target_only(self):
        deployments = [item for item in resources() if item.get("kind") == "Deployment"
                       and item["metadata"]["name"] in APPLICATIONS]
        cart = readiness(deployments, "cartservice")
        self.assertNotIn("cartservice", cart)
        self.assertIs(cart["shippingservice"], True)
        self.assertEqual(set(cart), APPLICATIONS - {"cartservice"})
        self.assertEqual(set(readiness(deployments)), APPLICATIONS - {"shippingservice"})
        with tempfile.TemporaryDirectory() as directory:
            experiment = connected_lab(directory, "cartservice")
            try:
                experiment.graph = configured_graph(connected_manifest(
                    resources(), {name + ":1": "e/" + name + "@sha256:" + "a" * 64 for name in APPLICATIONS},
                    "sentinel-boutique-connected"))
                target = next(item for item in deployments if item["metadata"]["name"] == "cartservice")
                target["metadata"].update(uid="cart-uid", namespace=experiment.namespace, resourceVersion="7")
                experiment.kubectl = Mock(return_value=SimpleNamespace(stdout=json.dumps({"items": deployments})))
                proposal = {"schema_version": "1.0", "kind": "proposal", "incident_id": "INC-1",
                            "action_id": "ACT-1", "action": "ROLLBACK", "based_on_resource_version": "7",
                            "target": {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace,
                                       "kind": "Deployment", "name": "cartservice", "uid": "cart-uid"},
                            "parameters": {"baseline_id": "B", "template_sha256": "a" * 64},
                            "rationale": "test", "evidence_refs": ["E"]}
                saved = experiment.dependency_result(proposal)
                state = json.loads((experiment.directory / "dependency-readiness.json").read_text(encoding="utf-8"))
            finally:
                experiment.store.close()
        self.assertNotIn("cartservice", state["known_health"])
        self.assertEqual((saved["status"], saved["target"]["name"]), ("CLEAR", "cartservice"))
        self.assertIn("cartservice", saved["affected_services"])

    def test_executor_role_lists_exactly_the_enabled_targets(self):
        for scope, names in (("connected", ["cartservice", "shippingservice"]), ("lab", ["shippingservice"]),
                             ("package", ["shippingservice"])):
            with self.subTest(scope=scope):
                self.assertEqual(list(targets.enabled_targets(scope)), names)
                text = targets.executor_rbac("sentinel-boutique", targets.enabled_targets(scope))
                self.assertEqual([line.strip() for line in text.splitlines() if "resourceNames" in line],
                                 ["resourceNames: " + json.dumps(names)])
                self.assertNotIn("demo-api", text)
                self.assertNotIn("sentinel-demo", text)
        for target in ("shippingservice", "cartservice"):
            with self.subTest(target=target):
                experiment = self.lab(target)
                experiment.write_executor_rbac()
                written = experiment.rbac_path.read_text(encoding="utf-8")
                self.assertIn('resourceNames: ["cartservice", "shippingservice"]', written)
                self.assertIn("namespace: sentinel-boutique-connected", written)
                self.assertEqual(experiment.executor_names(), ["cartservice", "shippingservice"])
        with self.assertRaises(CatalogError):
            targets.executor_rbac("sentinel-boutique", [])
        with self.assertRaises(CatalogError):
            targets.executor_rbac("sentinel-boutique", ["shippingservice"],
                                  template='resourceNames: ["demo-api"]\nresourceNames: ["x"]\n')

    def test_isolated_lab_role_is_unchanged_for_shipping(self):
        # The text the harness wrote before the catalog: replace the namespace and demo-api with the target.
        template = (ROOT / "deploy/sentinel/executor-rbac.yaml").read_text(encoding="utf-8")
        before = template.replace("sentinel-demo", "sentinel-boutique").replace("demo-api", "shippingservice")
        self.assertEqual(targets.executor_rbac("sentinel-boutique", ["shippingservice"]), before)

    def test_cart_target_takes_its_profile_from_the_catalog(self):
        experiment = self.lab("cartservice")
        self.assertEqual((experiment.name, experiment.service_account), ("cartservice", "cartservice"))
        self.assertEqual(experiment.allowed_registries, ["us-central1-docker.pkg.dev"])
        self.assertEqual(experiment.ceilings["container_requests"]["cpu"], "200m")
        self.assertEqual(experiment.scenario["id"], "C1")
        self.assertEqual(experiment.summary["recovery_target"], "cartservice")
        self.assertEqual(experiment.baseline_inputs(), {"baseline": None, "baselines": experiment.directory / "baselines"})
        self.assertIn(("patch", "deployment/paymentservice"), experiment.denied_permissions())
        shipping = self.lab()
        self.assertEqual((shipping.service_account, shipping.scenario["id"]), ("shippingservice", "R1"))
        self.assertEqual(shipping.ceilings["container_requests"]["cpu"], "100m")
        experiment.target = {"name": "cartservice"}
        with self.assertRaisesRegex(RuntimeError, "cannot change after preparation"):
            experiment.select_target("shippingservice")
        with self.assertRaisesRegex(CatalogError, "Unknown recovery target"):
            shipping.select_target("paymentservice")
        self.assertEqual(shipping.name, "shippingservice")

    def test_every_executor_entry_reads_the_per_target_baselines_directory(self):
        from experiments.recovery import live as recovery
        experiment = self.lab("cartservice")
        folder = experiment.directory / "baselines"
        self.assertEqual(experiment.baseline_arguments(), ["--baselines", str(folder)])
        config = experiment.directory / "worker.json"
        config.write_text(json.dumps({"directory": str(experiment.directory), "folder": str(experiment.directory),
                                      "credentials": "unused", "cluster_id": experiment.cluster_id,
                                      "context": experiment.restricted_context, "baselines": str(folder)}),
                          encoding="utf-8")
        with patch.object(recovery, "build_adapters", side_effect=RuntimeError("stop before Kubernetes")) as build, \
                patch.object(recovery, "executor_client_from_environment", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "stop before Kubernetes"):
                recovery.crash_worker(config)
        self.assertEqual(build.call_args.kwargs["baselines"], str(folder))
        self.assertNotIn("baseline", build.call_args.kwargs)

    def test_cart_is_not_offered_in_the_isolated_lab(self):
        experiment = BoutiqueExperiment(Path(self.directory) / "isolated")
        self.addCleanup(experiment.store.close)
        experiment.select_target("cartservice")
        with self.assertRaisesRegex(RuntimeError, "not offered in this lab scope"):
            experiment.executor_names()
        experiment.prepared = None
        with patch("experiments.boutique.harness.live.command", return_value="sentinel-boutique-lab-control-plane"):
            with self.assertRaisesRegex(RuntimeError, "no isolated lab manifest"):
                experiment.prepare_image()

    def test_execution_scope_lists_the_measured_identity_of_every_enabled_target(self):
        experiment = self.lab("cartservice")
        experiment.target = {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace,
                             "kind": "Deployment", "name": "cartservice", "uid": "cart-uid"}
        experiment.kubectl = Mock(return_value=SimpleNamespace(stdout=json.dumps({"metadata": {"uid": "ship-uid"}})))
        self.assertEqual(experiment.execution_scope(), [
            {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace, "kind": "Deployment",
             "name": "cartservice", "uid": "cart-uid"},
            {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace, "kind": "Deployment",
             "name": "shippingservice", "uid": "ship-uid"}])
        experiment.kubectl.assert_called_once_with("get", "deployment", "shippingservice", "-o", "json")

    def test_permissions_check_both_targets_and_deny_other_boutique_writes(self):
        experiment = self.lab("cartservice")
        asked = []

        def kubectl(*arguments, **options):
            if arguments[:2] == ("auth", "can-i"):
                asked.append(arguments[2:4])
                allowed = arguments[3] in ("deployment/cartservice", "deployment/shippingservice") \
                    and arguments[2] in ("get", "patch")
                return SimpleNamespace(stdout="yes\n" if allowed else "no\n", stderr="", returncode=0)
            return SimpleNamespace(stdout="", stderr="Error: forbidden", returncode=1)

        experiment.kubectl = kubectl
        experiment.check = Mock()
        experiment.permissions()
        self.assertEqual(asked[:4], [("get", "deployment/cartservice"), ("patch", "deployment/cartservice"),
                                     ("get", "deployment/shippingservice"), ("patch", "deployment/shippingservice")])
        self.assertIn(("patch", "deployment/paymentservice"), asked)
        self.assertIn(("delete", "deployment/cartservice"), asked)

    def test_storefront_impact_is_the_catalog_prediction_per_target(self):
        cart, shipping = targets.profile("cartservice"), targets.profile("shippingservice")
        self.assertTrue(targets.impact_matches(cart, CART_FAULT))
        self.assertFalse(targets.impact_matches(cart, SHIPPING_FAULT))
        self.assertTrue(targets.impact_matches(shipping, SHIPPING_FAULT))
        self.assertFalse(targets.impact_matches(shipping, CART_FAULT))
        self.assertEqual(targets.impact_phrase(shipping), "cart and checkout fail")
        for target, journey, check in (
                ("shippingservice", SHIPPING_FAULT, "Connected cart and checkout fail while shipping configuration is broken"),
                ("cartservice", CART_FAULT,
                 "Connected home, add to cart, cart and checkout fail while cart configuration is broken")):
            with self.subTest(target=target):
                experiment = self.lab(target)
                experiment.storefront = Mock()
                experiment.storefront.journey.return_value = dict(journey)
                experiment.record_shopping = Mock()
                with patch.object(BoutiqueExperiment, "fault", return_value={"incident_id": "INC-1"}):
                    self.assertEqual(experiment.fault("primary"), {"incident_id": "INC-1"})
                self.assertEqual(experiment.summary["checks"][-1]["name"], check)
                wrong = CART_FAULT if target == "shippingservice" else SHIPPING_FAULT
                experiment.storefront.journey.return_value = dict(wrong)
                with patch.object(BoutiqueExperiment, "fault", return_value={"incident_id": "INC-2"}), \
                        self.assertRaisesRegex(RuntimeError, "Storefront impact differs from the catalog prediction"):
                    experiment.fault("crash")

    def test_cart_probe_uses_the_vendored_proto_and_shipping_probe_is_unchanged(self):
        cart = self.lab("cartservice")
        cart.client_image = "docker.io/fullstorydev/grpcurl@sha256:" + "c" * 64
        pod = cart.probe_pod("cart-probe-1", "hipstershop.CartService/GetCart", {"userId": "u"})
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["args"], ["-plaintext", "-max-time", "5", "-import-path", "/proto", "-proto",
                                             "cart.proto", "-d", '{"userId": "u"}', "cartservice:7070",
                                             "hipstershop.CartService/GetCart"])
        self.assertEqual(pod["spec"]["volumes"], [{"name": "proto", "configMap": {"name": "sentinel-probe-proto"}}])
        self.assertEqual(container["volumeMounts"], [{"name": "proto", "mountPath": "/proto", "readOnly": True}])
        shipping = self.lab()
        shipping.client_image = cart.client_image
        request = {"items": [{"productId": "demo-item", "quantity": 1}]}
        # The probe Pod the shipping harness always created.
        self.assertEqual(shipping.probe_pod("shipping-probe-1", "hipstershop.ShippingService/GetQuote", request), {
            "apiVersion": "v1", "kind": "Pod", "metadata": {"name": "shipping-probe-1", "namespace": shipping.namespace},
            "spec": {"restartPolicy": "Never", "activeDeadlineSeconds": 45, "automountServiceAccountToken": False,
                     "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                         "seccompProfile": {"type": "RuntimeDefault"}},
                     "containers": [{"name": "probe", "image": cart.client_image, "imagePullPolicy": "Never",
                         "args": ["-plaintext", "-max-time", "5", "-d", json.dumps(request),
                                  "shippingservice:50051", "hipstershop.ShippingService/GetQuote"],
                         "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                       "limits": {"cpu": "100m", "memory": "64Mi"}},
                         "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                             "capabilities": {"drop": ["ALL"]}}}]}})
        shipping.kubectl = Mock()
        shipping.install_probe_schema()  # Shipping uses server reflection: nothing to publish.
        shipping.kubectl.assert_not_called()
        cart.kubectl = Mock()
        cart.install_probe_schema()
        configmap = json.loads((cart.directory / "probe-proto.json").read_text(encoding="utf-8"))
        self.assertEqual(configmap["metadata"], {"name": "sentinel-probe-proto", "namespace": cart.namespace})
        self.assertIn("service CartService", configmap["data"]["cart.proto"])

    def test_cart_round_trip_reaches_redis_and_is_validated(self):
        cart = self.lab("cartservice")
        calls = []

        def grpc_call(method, request):
            calls.append((method, request))
            if method.endswith("GetCart"):
                return {"userId": request["userId"], "items": [{"productId": "OLJCESPC7Z", "quantity": 1}]}
            return {}

        cart.grpc_call = grpc_call
        response = cart.probe()
        user = response["user_id"]
        self.assertTrue(user.startswith("sentinel-probe-"))
        self.assertEqual([method.rsplit("/", 1)[1] for method, _ in calls], ["AddItem", "GetCart", "EmptyCart"])
        self.assertEqual({request["userId"] for _, request in calls}, {user})
        good = {"user_id": user, "cart": {"userId": user, "items": [{"productId": "OLJCESPC7Z", "quantity": 1}]},
                "emptied": True}
        self.assertEqual(validate_cart(good), good["cart"])
        for change in ({"cart": {"userId": "someone-else", "items": good["cart"]["items"]}},
                       {"cart": {"userId": user, "items": []}},
                       {"cart": {"userId": user, "items": good["cart"]["items"] * 2}},
                       {"user_id": "other-user", "cart": {"userId": "other-user", "items": good["cart"]["items"]}},
                       {"emptied": False}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                validate_cart({**good, **change})


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


class PreparedRunInputsTests(unittest.TestCase):
    """prepare() writes the policy and per-target baseline the live Executor reads (P3.09).

    kubectl, the image step and the health window are fake; the Guard and the
    Executor are the real ones, reading only the files prepare() wrote.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted")),
                        patch("runtime.live_executor.read_reference_versions", return_value=[])):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def template(name, cpu):
        """A reviewed-style healthy Pod template for one catalog target."""
        profile = targets.profile(name)
        env = [{"name": "REDIS_ADDR", "value": "redis-cart:6379"} if name == "cartservice"
               else {"name": "PORT", "value": str(profile.port)}]
        return {"metadata": {"labels": {"app": name}}, "spec": {
            "serviceAccountName": profile.service_account, "automountServiceAccountToken": False,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": profile.container, "image": profile.image_repository + "@sha256:" + "a" * 64,
                            "imagePullPolicy": "Never", "env": env,
                            "resources": {"requests": {"cpu": cpu, "memory": "64Mi"},
                                          "limits": {"cpu": "300m", "memory": "128Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                "capabilities": {"drop": ["ALL"]}}}]}}

    def deployment(self, experiment, name, template, version="7", ready=1):
        return {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": name, "namespace": experiment.namespace, "uid": name + "-uid",
                             "resourceVersion": version, "generation": 1},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}}, "template": deepcopy(template)},
                "status": {"observedGeneration": 1, "replicas": 1, "readyReplicas": ready, "availableReplicas": ready}}

    def prepared(self, target, cpu):
        """Run the real prepare() for target, whose healthy template requests cpu."""
        experiment = connected_lab(self.directory / target / cpu, target)
        self.addCleanup(experiment.store.close)
        deployments = {name: self.deployment(experiment, name, self.template(name, cpu if name == target else "100m"))
                       for name in targets.enabled_targets("connected")}
        commands = []

        def kubectl(*arguments, **options):
            commands.append(arguments)
            if arguments[:2] == ("get", "deployment") and arguments[2] in deployments:
                return SimpleNamespace(stdout=json.dumps(deployments[arguments[2]]), stderr="", returncode=0)
            if arguments[:2] == ("config", "view"):
                cluster = {"server": "https://127.0.0.1:6443", "certificate-authority-data": "b2ZmbGluZQ=="}
                return SimpleNamespace(stdout=json.dumps({"clusters": [{"cluster": cluster}]}), stderr="", returncode=0)
            if arguments[:2] == ("create", "token") or arguments[0] == "apply":
                return SimpleNamespace(stdout="offline-token\n", stderr="", returncode=0)
            raise AssertionError("Unexpected kubectl call: " + " ".join(arguments))

        def prepare_image():
            experiment.write_executor_rbac()
            return deployments[target]["spec"]["template"]["spec"]["containers"][0]["image"]

        start = datetime.now(timezone.utc) - timedelta(seconds=21)
        samples = [{"at": iso(start + timedelta(seconds=10 * index)), "ready_replicas": 1, "resource_version": "7"}
                   for index in range(3)]
        experiment.kubectl = kubectl
        experiment.prepare_image = prepare_image
        experiment.health_window = Mock(return_value=samples)
        with redirect_stdout(StringIO()):
            experiment.prepare()
        return experiment, commands

    def read(self, experiment, *parts):
        return json.loads(experiment.directory.joinpath(*parts).read_text(encoding="utf-8"))

    def run_executor(self, experiment, name=None):
        """Ask the real Executor, built from prepare()'s files, to roll name back; returns (decision, result, writes)."""
        from runtime.live_executor import build_adapters
        from services.executor.core import execute
        from services.guard.core import proposal_fingerprint
        from tests.support.executor_fake import FakeDeploymentAPI

        name = name or experiment.name
        scope = {item["name"]: item for item in self.read(experiment, "allowed-targets.json")}
        target = scope[name]
        baseline = self.read(experiment, "baselines", name + ".json")
        current = datetime.now(timezone.utc)
        # The bad rollout: the measured healthy template with only its configuration changed.
        broken = deepcopy(baseline["pod_template"])
        broken["spec"]["containers"][0]["env"][0]["value"] += "0"
        live = self.deployment(experiment, name, broken, version="8", ready=0)
        live["metadata"]["uid"] = target["uid"]
        snapshot = {"source": "offline-test", "captured_at": iso(current), "cluster_id": experiment.cluster_id,
                    "deployment": live, "pods": [{"apiVersion": "v1", "kind": "Pod",
                        "metadata": {"name": name + "-1", "namespace": experiment.namespace, "uid": "pod-uid",
                                     "labels": {"app": name}},
                        "status": {"phase": "Running", "containerStatuses": [{
                            "name": "server", "ready": False, "restartCount": 4,
                            "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "Back-off"}},
                            "lastState": {"terminated": {"exitCode": 1, "reason": "Error"}}}]}}],
                    "events": [], "logs": []}
        proposal = {"schema_version": "1.0", "kind": "proposal", "incident_id": "INC-PREPARED-1",
                    "action_id": "ACT-PREPARED-1", "action": "ROLLBACK", "target": target,
                    "parameters": {"baseline_id": baseline["baseline_id"], "template_sha256": baseline["template_sha256"]},
                    "based_on_resource_version": "8", "rationale": "Offline test of the prepared run inputs.",
                    "evidence_refs": ["E-1"]}
        inputs = Path(tempfile.mkdtemp(prefix="inputs-", dir=self.directory))
        dependencies, approval = inputs / "dependencies.json", inputs / "approval.json"
        dependencies.write_text(json.dumps({
            "schema_version": "1.0", "kind": "dependency_result", "incident_id": proposal["incident_id"],
            "action_id": proposal["action_id"], "target": target, "status": "CLEAR", "affected_services": [name],
            "blockers": [], "unknowns": [], "checked_at": iso(current)}), encoding="utf-8")
        approval.write_text(json.dumps({
            "action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
            "policy_version": self.read(experiment, "policy.json")["policy_version"], "operator_id": "offline-operator",
            "decision": "APPROVED", "approved_at": iso(current - timedelta(seconds=2)),
            "expires_at": iso(current + timedelta(seconds=120))}), encoding="utf-8")
        cluster = FakeDeploymentAPI(snapshot, clock=lambda: datetime.now(timezone.utc))
        with patch("services.executor.kubernetes_adapter.KubectlDeploymentAPI", return_value=cluster):
            adapters = build_adapters(
                context=experiment.restricted_context, cluster_id=experiment.cluster_id,
                kubeconfig=experiment.credentials, allowed_targets=experiment.directory / "allowed-targets.json",
                ledger=inputs / "executor.db", locks=inputs / "locks", policy=experiment.directory / "policy.json",
                dependencies=dependencies, approval=approval, **experiment.baseline_inputs())
            self.addCleanup(adapters.ledger.close)
            decision = adapters.authorizer.validate(proposal, cluster.read(target), now=current).decision
            result = execute(proposal, adapters)
        return decision, result, cluster.writes

    def test_a_cart_run_writes_the_cart_ceilings_and_its_guard_allows_the_cart_baseline(self):
        experiment, _commands = self.prepared("cartservice", "200m")
        policy = self.read(experiment, "policy.json")
        self.assertEqual(policy["resource_constraints"]["max_container_requests"], {"cpu": "200m", "memory": "64Mi"})
        self.assertEqual(policy["workload_constraints"]["max_pod_requests"], {"cpu": "250m", "memory": "128Mi"})
        self.assertEqual(policy["security_constraints"]["allowed_service_accounts"], ["cartservice"])
        self.assertEqual(policy["security_constraints"]["allowed_image_registries"], ["us-central1-docker.pkg.dev"])
        decision, result, writes = self.run_executor(experiment)
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(writes), 1)

    def test_a_cart_run_guard_rejects_an_over_ceiling_cart_baseline_with_zero_writes(self):
        experiment, _commands = self.prepared("cartservice", "400m")
        decision, result, writes = self.run_executor(experiment)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("RESOURCE_REQUEST_EXCEEDS_POLICY", decision["reason_codes"])
        self.assertNotEqual(result["status"], "APPLIED")
        self.assertEqual(writes, [])

    def test_a_shipping_run_still_writes_the_shipping_ceilings(self):
        experiment, _commands = self.prepared("shippingservice", "100m")
        policy = self.read(experiment, "policy.json")
        self.assertEqual(policy["resource_constraints"]["max_container_requests"], {"cpu": "100m", "memory": "64Mi"})
        self.assertEqual(policy["security_constraints"]["allowed_service_accounts"], ["shippingservice"])
        decision, result, writes = self.run_executor(experiment)
        self.assertEqual((decision["verdict"], result["status"], len(writes)), ("ALLOW", "APPLIED", 1))
        # The cart baseline request would exceed the shipping ceiling.
        experiment, _commands = self.prepared("shippingservice", "200m")
        decision, _result, writes = self.run_executor(experiment)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("RESOURCE_REQUEST_EXCEEDS_POLICY", decision["reason_codes"])
        self.assertEqual(writes, [])

    def test_prepare_writes_the_selected_targets_baseline_file_that_the_executor_reads(self):
        for name in ("cartservice", "shippingservice"):
            with self.subTest(target=name):
                experiment, _commands = self.prepared(name, "100m")
                baseline = self.read(experiment, "baseline.json")
                self.assertEqual(self.read(experiment, "baselines", name + ".json"), baseline)
                self.assertEqual(baseline["target"], experiment.target)
                self.assertEqual(baseline["target"]["name"], name)
                self.assertEqual(sorted(path.name for path in (experiment.directory / "baselines").iterdir()),
                                 [name + ".json"])
                self.assertEqual(experiment.baseline_inputs(),
                                 {"baseline": None, "baselines": experiment.directory / "baselines"})
                decision, result, writes = self.run_executor(experiment)
                self.assertEqual((decision["verdict"], result["status"], len(writes)), ("ALLOW", "APPLIED", 1))
                self.assertEqual(writes[0]["deployment"]["spec"]["template"], baseline["pod_template"])

    def test_the_policy_names_only_the_selected_target_while_the_allowlist_names_both(self):
        experiment, commands = self.prepared("cartservice", "200m")
        policy = self.read(experiment, "policy.json")
        scope = self.read(experiment, "allowed-targets.json")
        self.assertEqual(policy["targets"], [experiment.target])
        self.assertEqual([item["name"] for item in scope], ["cartservice", "shippingservice"])
        self.assertEqual(scope[1]["uid"], "shippingservice-uid")
        self.assertIn('resourceNames: ["cartservice", "shippingservice"]',
                      (experiment.directory / "executor-rbac.yaml").read_text(encoding="utf-8"))
        self.assertIn(("apply", "-f", str(experiment.directory / "executor-rbac.yaml")), commands)
        # A baseline planted for the other enabled target still meets the policy target list.
        other = self.prepared("shippingservice", "100m")[0]
        planted = self.read(other, "baselines", "shippingservice.json")
        planted["target"] = scope[1]
        (experiment.directory / "baselines" / "shippingservice.json").write_text(json.dumps(planted), encoding="utf-8")
        decision, result, writes = self.run_executor(experiment, "shippingservice")
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("TARGET_NOT_ALLOWED", decision["reason_codes"])
        self.assertNotEqual(result["status"], "APPLIED")
        self.assertEqual(writes, [])


class StorefrontHTTPTests(unittest.TestCase):
    def setUp(self):
        self.cookies = []
        self.success = True
        self.confirmation = True
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def send(self, code, body):
                self.send_response(code)
                if self.path == "/":
                    self.send_header("Set-Cookie", "sid=session-" + str(len(owner.cookies)))
                data = body.encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.path == "/": self.send(200, "catalog")
                elif self.path == "/cart": self.send(200 if owner.success else 500, "OLJCESPC7Z shipping estimate")
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/cart":
                    owner.cookies.append(self.headers.get("Cookie"))
                    self.send(302, "added")
                else:
                    body = ("<h3>Your order is complete!</h3><div>Confirmation #</div><div>123e4567-e89b-12d3-a456-426614174000</div>"
                            if owner.confirmation else "Your order is complete! Confirmation # unavailable Footer session-id: 123e4567-e89b-12d3-a456-426614174000")
                    self.send(200 if owner.success else 500, body)
            def log_message(self, *args): pass
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.probe = StorefrontProbe(f"http://127.0.0.1:{self.server.server_port}")
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(2)

    def test_full_journey_requires_actual_cart_and_order_confirmation_with_fresh_sessions(self):
        for _ in range(2):
            result = self.probe.journey()
            self.assertTrue(result["passed"])
            self.assertEqual(result["order_confirmation_id"], "123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(len(set(self.cookies)), 2)

    def test_frontend_alive_does_not_hide_cart_and_checkout_failure(self):
        self.success = False
        result = self.probe.journey()
        self.assertEqual(result["home_status"], 200)
        self.assertEqual(result["add_to_cart_status"], 302)
        self.assertEqual(result["cart_status"], 500)
        self.assertEqual(result["checkout_status"], 500)
        self.assertFalse(result["passed"])

    def test_http_200_without_order_confirmation_is_not_success(self):
        self.confirmation = False
        self.assertFalse(self.probe.journey()["passed"])

    def test_probe_cannot_target_public_or_credentialed_urls(self):
        for url in ("https://store.example", "http://store.example:8080", "http://user:secret@127.0.0.1:8080",
                    "http://127.0.0.1:8080/other"):
            with self.assertRaises(ValueError):
                StorefrontProbe(url)


class ShopOutcomeTests(unittest.TestCase):
    """The whole-storefront aggregate of a pair (P4.05) is separate from each incident's outcome."""

    def test_recovered_needs_both_incidents_and_three_passing_journeys_in_a_row(self):
        from experiments.boutique.harness.multi_fault import shop_outcome
        both = {"shippingservice": "RECOVERED", "cartservice": "RECOVERED"}
        result = shop_outcome(both, [{"passed": False}] + [{"passed": True}] * 3)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertEqual(result["storefront"], {"journeys": 4, "passed": 3, "required_passes": 3, "complete": True,
                                                "sustained": True})
        self.assertEqual(shop_outcome(both, [{"passed": True}] * 2 + [{"passed": False}])["status"], "FAILED")

    def test_partial_when_one_target_recovers_and_the_other_fails(self):
        from experiments.boutique.harness.multi_fault import shop_outcome
        for journeys in ([{"passed": True}] * 3, [{"passed": False}] * 3, []):
            with self.subTest(journeys=len(journeys)):
                result = shop_outcome({"shippingservice": "RECOVERED", "cartservice": "FAILED"}, journeys)
                self.assertEqual((result["status"], result["not_recovered"]), ("PARTIAL", ["cartservice"]))
        self.assertEqual(shop_outcome({"shippingservice": "REJECTED", "cartservice": "NEEDS_REVIEW"},
                                      [{"passed": True}] * 3)["status"], "FAILED")

    def test_unknown_on_missing_storefront_samples_or_an_unsettled_incident(self):
        from experiments.boutique.harness.multi_fault import shop_outcome
        both = {"shippingservice": "RECOVERED", "cartservice": "RECOVERED"}
        for journeys in ([], [{"passed": True}] * 2, [{"passed": True}, {"passed": None}, {"passed": True}],
                         [{"passed": True}, "lost", {"passed": True}]):
            with self.subTest(journeys=journeys):
                self.assertEqual(shop_outcome(both, journeys)["status"], "UNKNOWN")
        result = shop_outcome({"shippingservice": "RECOVERED", "cartservice": "VERIFYING"}, [{"passed": True}] * 3)
        self.assertEqual((result["status"], result["reason"]), ("UNKNOWN", "incident not settled: cartservice"))
        for states, passes in (({}, 3), (both, 0), (both, "3")):
            with self.subTest(states=states, passes=passes), self.assertRaises(ValueError):
                shop_outcome(states, [], required_passes=passes)

    def test_an_incident_the_runner_stopped_counts_as_settled_and_not_recovered(self):
        from experiments.boutique.harness.multi_fault import shop_outcome
        # An escalated or refused approval leaves the Coordinator at AWAITING_APPROVAL; the runner never acts again.
        states = {"shippingservice": "RECOVERED", "cartservice": "AWAITING_APPROVAL"}
        self.assertEqual(shop_outcome(states, [{"passed": True}] * 3)["status"], "UNKNOWN")
        result = shop_outcome(states, [{"passed": True}] * 3, stopped={"cartservice": "ESCALATED"})
        self.assertEqual((result["status"], result["not_recovered"]), ("PARTIAL", ["cartservice"]))
        result = shop_outcome({"shippingservice": "AWAITING_APPROVAL", "cartservice": "AWAITING_APPROVAL"}, [],
                              stopped={"shippingservice": "APPROVAL_REFUSED", "cartservice": "ESCALATED"})
        self.assertEqual(result["status"], "FAILED")
        # A stop of the other target does not settle one still in progress.
        result = shop_outcome({"shippingservice": "VERIFYING", "cartservice": "AWAITING_APPROVAL"}, [],
                              stopped={"cartservice": "ESCALATED"})
        self.assertEqual((result["status"], result["reason"]), ("UNKNOWN", "incident not settled: shippingservice"))


GRAPH = {"edge_direction": "caller_to_callee", "nodes": sorted(APPLICATIONS),
         "edges": [{"from": "frontend", "to": "cartservice"}, {"from": "frontend", "to": "shippingservice"},
                   {"from": "checkoutservice", "to": "cartservice"}, {"from": "cartservice", "to": "redis-cart"}]}


class TargetVerificationScopeTests(unittest.TestCase):
    """A pair verifies each target by its own probe and callees; single-target runs keep the shop scope."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.experiment = connected_lab(temporary.name, "cartservice")
        self.addCleanup(self.experiment.store.close)
        self.experiment.graph = deepcopy(GRAPH)

    def test_verification_dependencies_are_the_direct_callees(self):
        from experiments.boutique.harness.connected import verification_dependencies
        self.assertEqual(verification_dependencies(GRAPH, "cartservice"), ["redis-cart"])
        self.assertEqual(verification_dependencies(GRAPH, "frontend"), ["cartservice", "shippingservice"])
        self.assertEqual(verification_dependencies(GRAPH, "redis-cart"), [])
        with self.assertRaises(ValueError):
            verification_dependencies(dict(GRAPH, edge_direction="callee_to_caller"), "cartservice")
        self.assertEqual(ConnectedBoutiqueExperiment.verification_scope, "shop")

    def samples(self, *, shipping_ready, redis_ready, storefront):
        experiment = self.experiment
        deployments = [item for item in resources() if item.get("kind") == "Deployment"
                       and item["metadata"]["name"] in APPLICATIONS]
        for item in deployments:
            name = item["metadata"]["name"]
            if (name == "shippingservice" and not shipping_ready) or (name == "redis-cart" and not redis_ready):
                item["status"]["readyReplicas"] = item["status"]["availableReplicas"] = 0
        target = next(item for item in deployments if item["metadata"]["name"] == "cartservice")
        target["metadata"]["uid"] = "cart-uid"
        identity = {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace,
                    "kind": "Deployment", "name": "cartservice", "uid": "cart-uid"}
        digest = fingerprint(target["spec"]["template"])
        execution = {"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-1",
                     "action_id": "ACT-1", "target": identity, "status": "APPLIED", "resource_version": "2",
                     "recorded_at": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                     "details": "Offline test execution"}
        sample = {"target": identity, "service": "cartservice", "protocol": "grpc", "grpc_status": 0,
                  "application_check_passed": True, "ready_replicas": 1, "available_replicas": 1,
                  "desired_replicas": 1, "template_sha256": digest, "configuration_stable": True}
        experiment.kubectl = Mock(return_value=SimpleNamespace(stdout=json.dumps({"items": deployments})))
        experiment.storefront = Mock()
        experiment.storefront.journey.return_value = {"passed": storefront}
        with patch.object(BoutiqueExperiment, "verification_samples",
                          return_value=iter([deepcopy(sample) for _ in range(3)])):
            measured = list(experiment.verification_samples(execution))
        start = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
        for index, reading in enumerate(measured):  # Readings one second apart, so the window is measurable.
            reading["at"] = (start + timedelta(seconds=index + 1)).isoformat().replace("+00:00", "Z")
        policy = {"health": {"protocol": "grpc", "required_grpc_status": 0, "minimum_samples": 3,
                             "stable_window_seconds": 1, "max_sample_gap_seconds": 90,
                             "required_dependencies": experiment.required_dependencies(),
                             "require_template_match": True, "expected_template_sha256": digest,
                             "require_target_identity": True}}
        return execution, measured, policy

    def test_target_scope_ignores_the_storefront_and_unrelated_services(self):
        self.experiment.verification_scope = "target"
        execution, measured, policy = self.samples(shipping_ready=False, redis_ready=True, storefront=False)
        self.assertEqual(policy["health"]["required_dependencies"], ["redis-cart"])
        self.assertEqual(measured[0]["dependencies"], {"redis-cart": True})
        self.assertIs(measured[0]["storefront"]["passed"], False)  # Still recorded, for the ShopOutcome.
        self.assertEqual(verify(execution, measured, policy)["status"], "RECOVERED")

    def test_the_shop_scope_still_fails_the_same_repair(self):
        execution, measured, policy = self.samples(shipping_ready=False, redis_ready=True, storefront=False)
        self.assertIn("storefront-checkout", policy["health"]["required_dependencies"])
        self.assertIs(measured[0]["dependencies"]["shippingservice"], False)
        self.assertEqual(verify(execution, measured, policy)["status"], "NOT_RECOVERED")

    def test_target_scope_still_fails_when_a_callee_is_not_ready(self):
        self.experiment.verification_scope = "target"
        execution, measured, policy = self.samples(shipping_ready=True, redis_ready=False, storefront=True)
        self.assertEqual(measured[0]["dependencies"], {"redis-cart": False})
        self.assertEqual(verify(execution, measured, policy)["status"], "NOT_RECOVERED")


class RedisProbeTests(unittest.TestCase):
    """redis-cart (pair only): a redis-cli PING from the target's own pinned image."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        for patcher in (patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted"))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.experiment = connected_lab(temporary.name, "redis-cart")
        self.addCleanup(self.experiment.store.close)
        image = "docker.io/library/redis@sha256:" + "c" * 64
        self.template = {"metadata": {"labels": {"app": "redis-cart"}},
                         "spec": {"containers": [{"name": "redis", "image": image}]}}
        self.experiment.baseline = {"pod_template": deepcopy(self.template)}

    def test_the_probe_pod_pings_the_service_from_the_pinned_image(self):
        pod = self.experiment.redis_probe_pod("redis-probe-1")
        container = pod["spec"]["containers"][0]
        self.assertEqual(container["command"], ["redis-cli", "-h", "redis-cart", "-p", "6379", "PING"])
        self.assertEqual((container["image"], container["imagePullPolicy"]),
                         (self.template["spec"]["containers"][0]["image"], "Never"))
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertIs(pod["spec"]["automountServiceAccountToken"], False)
        self.experiment.baseline["pod_template"]["spec"]["containers"][0]["image"] = "redis:alpine"
        with self.assertRaisesRegex(RuntimeError, "pinned by digest"):
            self.experiment.redis_probe_pod("redis-probe-2")

    def test_the_reply_is_kept_as_measured_and_only_pong_is_healthy(self):
        from experiments.boutique.harness.live import REDIS_REPLY_LIMIT, validate_redis
        run = Mock(side_effect=lambda name, pod, parse: parse(self.reply))
        self.experiment.run_probe_pod = run
        for self.reply, expected in (("PONG\n", "PONG"), ("x" * 200, "x" * (REDIS_REPLY_LIMIT + 1))):
            self.assertEqual(self.experiment.redis_response(), expected)
        self.assertEqual(validate_redis("PONG"), "PONG")
        for reply in ("pong", "PONG ", "", None):
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                validate_redis(reply)
        rules = self.experiment.health_rules()
        self.assertEqual((rules["protocol"], rules["required_redis_reply"]), ("redis", "PONG"))
        self.assertNotIn("required_grpc_status", rules)

    def test_a_verification_reading_carries_the_reply_or_a_collection_error(self):
        deployment = {"metadata": {"uid": "redis-uid", "resourceVersion": "5", "generation": 1},
                      "spec": {"replicas": 1, "template": deepcopy(self.template)},
                      "status": {"observedGeneration": 1, "readyReplicas": 1, "availableReplicas": 1}}
        experiment = self.experiment
        experiment.policy = {"health": experiment.health_rules()}
        experiment.deployment = Mock(return_value=deployment)
        experiment.observe = Mock()
        execution = {"target": {"name": "redis-cart"}, "incident_id": "INC-1", "action_id": "ACT-1"}
        for response, reply, error in ((Mock(return_value="PONG"), "PONG", None),
                                       (Mock(side_effect=RuntimeError("Redis RPC failed")), None, "RuntimeError")):
            experiment.probe_response = response
            with self.subTest(reply=reply), patch("time.sleep"):
                sample = next(BoutiqueExperiment.verification_samples(experiment, execution))
            self.assertEqual((sample["protocol"], sample["redis_reply"], sample.get("collection_error")),
                             ("redis", reply, error))
            self.assertNotIn("grpc_status", sample)


class PairHarnessTests(unittest.TestCase):
    """The live pair harness offline: lanes, routing, policies and refusals; nothing reaches a cluster."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for patcher in (patch("shared.provenance.collect", return_value={"source_revision": "test"}),
                        patch("subprocess.run", side_effect=AssertionError("cluster command attempted"))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def pair(self, pair_id="redis-pod+cart-config", **options):
        from experiments.boutique.harness.multi_fault import PairBoutiqueExperiment
        experiment = PairBoutiqueExperiment(self.directory / (pair_id + str(len(list(self.directory.iterdir())))),
                                            pair_id=pair_id, **options)
        self.addCleanup(experiment.store.close)
        experiment.graph = deepcopy(GRAPH)
        for name, lane in experiment.lanes.items():
            lane["target"] = {"cluster_id": experiment.cluster_id, "namespace": experiment.namespace,
                              "kind": "Deployment", "name": name, "uid": name + "-uid"}
        return experiment

    def test_lanes_follow_the_catalog_pair_and_its_order(self):
        dependency, independent = self.pair(), self.pair("shipping-config+cart-pod")
        self.assertEqual(dependency.order, ["redis-cart", "cartservice"])
        self.assertEqual({name: lane["scenario_id"] for name, lane in dependency.lanes.items()},
                         {"redis-cart": "D1", "cartservice": "C1"})
        self.assertEqual(independent.order, ["shippingservice", "cartservice"])
        # redis-cart joins the Executor Role only in its own pair.
        self.assertEqual(dependency.executor_names(), ["cartservice", "redis-cart", "shippingservice"])
        self.assertEqual(independent.executor_names(), ["cartservice", "shippingservice"])
        self.assertEqual(dependency.summary["pair"]["labels"], "pending")
        self.assertEqual(dependency.verification_scope, "target")

    def test_the_executor_kill_variant_kills_the_configuration_repair(self):
        self.assertEqual(self.pair(executor_kill=True).kill_target, "cartservice")
        self.assertEqual(self.pair("shipping-config+cart-pod", executor_kill=True).kill_target, "shippingservice")
        self.assertIsNone(self.pair().kill_target)

    def test_acting_as_switches_one_target_and_always_restores(self):
        experiment = self.pair()
        with experiment.acting_as("cartservice") as lane:
            self.assertEqual((experiment.name, experiment.scenario["id"], experiment.profile.label),
                             ("cartservice", "C1", "cart"))
            self.assertIs(experiment.target, lane["target"])
            with experiment.acting_as("redis-cart"):
                self.assertEqual(experiment.profile.probe["kind"], "redis")
            self.assertEqual(experiment.name, "cartservice")
        self.assertEqual((experiment.name, experiment.target), ("redis-cart", None))
        with self.assertRaises(KeyError), experiment.acting_as("cartservice"):
            raise KeyError("boom")
        self.assertEqual(experiment.name, "redis-cart")

    def test_each_target_gets_its_own_live_policy(self):
        from services.guard.decisions import validate_policy
        experiment = self.pair()
        redis, cart = (experiment.lane_policy(name) for name in ("redis-cart", "cartservice"))
        for name, policy in (("redis-cart", redis), ("cartservice", cart)):
            with self.subTest(name=name):
                validate_policy(policy, require_live=True)
                self.assertEqual(policy["targets"], [experiment.lanes[name]["target"]])
                self.assertEqual(policy["restart_constraints"]["targets"], [experiment.lanes[name]["target"]])
                self.assertEqual(policy["allowed_actions"], ["ROLLBACK", "RESTART"])
        self.assertEqual((redis["health"]["protocol"], redis["health"]["required_dependencies"]), ("redis", []))
        self.assertEqual((cart["health"]["protocol"], cart["health"]["required_dependencies"]), ("grpc", ["redis-cart"]))
        self.assertEqual(redis["security_constraints"]["allowed_service_accounts"], ["default"])
        self.assertEqual(cart["security_constraints"]["allowed_service_accounts"], ["cartservice"])
        self.assertEqual(redis["resource_constraints"]["max_container_limits"], {"cpu": "200m", "memory": "256Mi"})
        self.assertEqual(cart["resource_constraints"]["max_container_limits"], {"cpu": "500m", "memory": "128Mi"})

    def test_readers_route_by_the_message_target_not_the_acting_one(self):
        experiment = self.pair()
        seen = []
        with patch.object(ConnectedBoutiqueExperiment, "dependency_result",
                          lambda lab, proposal: seen.append(lab.name) or {"for": lab.name}):
            with experiment.acting_as("redis-cart"):
                self.assertEqual(experiment.dependency_result({"target": {"name": "cartservice"}}),
                                 {"for": "cartservice"})
                self.assertEqual(experiment.name, "redis-cart")
        self.assertEqual(seen, ["cartservice"])
        names = []

        def readings(lab, execution):
            for index in range(2):
                names.append(lab.name)
                yield {"index": index}
        with patch.object(ConnectedBoutiqueExperiment, "verification_samples", readings):
            stream = experiment.lane_samples({"target": {"name": "cartservice"}})
            self.assertEqual(next(stream), {"index": 0})
            self.assertEqual(experiment.name, "redis-cart")  # Nothing leaks between readings.
            self.assertEqual(list(stream), [{"index": 1}])
        self.assertEqual(names, ["cartservice", "cartservice"])

    def test_each_target_keeps_its_own_dependency_readiness_file(self):
        experiment = self.pair()
        deployments = [item for item in resources() if item.get("kind") == "Deployment"]
        deployments.append({"kind": "Deployment", "metadata": {"name": "redis-cart", "generation": 1},
                            "spec": {"replicas": 1}, "status": {"observedGeneration": 1, "readyReplicas": 0,
                                                                "availableReplicas": 0}})
        experiment.kubectl = Mock(return_value=SimpleNamespace(stdout=json.dumps({"items": deployments})))
        with patch("experiments.boutique.harness.connected.inspect_dependencies", return_value={"status": "CLEAR"}):
            for name in ("cartservice", "redis-cart"):
                experiment.dependency_result({"target": {"name": name}})
        saved = {name: json.loads((experiment.directory / ("dependency-readiness-" + name + ".json"))
                                  .read_text(encoding="utf-8")) for name in ("cartservice", "redis-cart")}
        self.assertEqual({name: state["deployment"]["metadata"]["name"] for name, state in saved.items()},
                         {"cartservice": "cartservice", "redis-cart": "redis-cart"})
        self.assertIs(saved["cartservice"]["known_health"]["redis-cart"], False)
        self.assertFalse((experiment.directory / "dependency-readiness.json").exists())

    def test_faults_applied_more_than_five_seconds_apart_fail_the_pair(self):
        from experiments.boutique.harness import multi_fault
        for chaos_at, passes in (("2026-11-13T10:00:03Z", True), ("2026-11-13T10:00:09Z", False)):
            experiment = self.pair()
            injected = []

            def inject(lab=experiment, at=chaos_at, injected=injected):
                injected.append(lab.name)
                if lab.scenario["injection"]["kind"] == "chaos":
                    lab.chaos_record = {"created_at": at}
            experiment.inject_fault = inject
            with self.subTest(chaos_at=chaos_at), patch.object(multi_fault, "live_utc",
                                                               return_value="2026-11-13T10:00:00Z"):
                if passes:
                    with redirect_stdout(StringIO()):
                        applied = experiment.inject_pair()
                    self.assertEqual(applied["redis-cart"], chaos_at)
                    self.assertEqual(experiment.summary["pair_injection"]["spread_seconds"], 3.0)
                else:
                    with self.assertRaisesRegex(RuntimeError, "9.0 s apart"):
                        experiment.inject_pair()
            self.assertEqual(injected, ["cartservice", "redis-cart"])  # The configuration fault goes first.

    def test_inject_pair_uses_the_chaos_harness_record_and_cleans_up_an_uncertain_create(self):
        from experiments.boutique.harness import chaos_recovery
        from tests.test_boutique_chaos import FakeInjectionCluster
        pod = "redis-cart-7c9d8f6b5-abcde"
        records = ["sentinel-boutique-connected/" + pod + "/redis"]
        for fail_create in (False, True):
            experiment = self.pair()
            fake = FakeInjectionCluster(records, {pod: ("uid-redis", "redis-cart")}, fail_create=fail_create)
            experiment.chaos_cluster = lambda fake=fake: fake
            configured = []
            clock = iter(range(0, 10_000, 25))
            with self.subTest(fail_create=fail_create), \
                    patch.object(ConnectedBoutiqueExperiment, "inject_fault",
                                 lambda lab, configured=configured: configured.append(lab.name)), \
                    patch.object(chaos_recovery, "time", SimpleNamespace(monotonic=lambda clock=clock: float(next(clock)),
                                                                         sleep=Mock())), \
                    redirect_stdout(StringIO()):
                if not fail_create:
                    applied = experiment.inject_pair()
                    self.assertEqual(applied["redis-cart"], experiment.chaos_record["created_at"])
                    self.assertEqual(experiment.chaos_record["selected_pods"], [{"name": pod, "uid": "uid-redis"}])
                    self.assertEqual(fake.calls[:2], [("create", "dry-run"), ("create", "real")])
                    self.assertIn("basis", experiment.summary["pair_injection"])
                else:
                    with self.assertRaisesRegex(RuntimeError, "connection reset"):
                        experiment.inject_pair()
                    # The object is known before the create call, so the pair's cleanup removes it.
                    self.assertIsNone(experiment.chaos_record)
                    experiment.remove_chaos()
                    self.assertEqual(fake.removed, [("PodChaos", experiment.chaos_document["metadata"]["name"])])
                self.assertEqual(configured, ["cartservice"])  # The configuration fault goes first.

    def test_one_target_runs_the_executor_at_a_time(self):
        experiment = self.pair()
        experiment.store = Mock()
        experiment.store.pending_routes.return_value = [(1,), (2,)]
        experiment.store.route.side_effect = lambda event_id: {"action_id": {1: "ACT-CART", 2: "ACT-REDIS"}[event_id]}
        with experiment.acting_as("cartservice"), self.assertRaisesRegex(RuntimeError, "one target runs at a time"):
            experiment.execute_lane({"action_id": "ACT-CART", "target": {"name": "cartservice"}})
        experiment.store.approval.assert_not_called()

    def test_emergency_restore_touches_only_changed_templates(self):
        from experiments.boutique.harness.multi_fault import Experiment
        experiment = self.pair()
        templates = {"redis-cart": {"spec": "redis"}, "cartservice": {"spec": "broken"}}
        for name in experiment.order:
            experiment.lanes[name]["baseline"] = {"template_sha256": fingerprint({"spec": name.split("-")[0]
                                                                                  if name == "redis-cart" else "good"})}
        experiment.deployment = lambda: {"spec": {"template": templates[experiment.name]}}
        experiment.remove_chaos = Mock(return_value=True)
        restored = []
        with patch.object(Experiment, "emergency_restore", lambda lab: restored.append(lab.name) or True):
            result = experiment.emergency_restore()
        self.assertEqual(result, {"redis-cart": "unchanged", "cartservice": True})
        self.assertEqual(restored, ["cartservice"])
        experiment.remove_chaos.assert_called_once()


class PairVerdictTests(unittest.TestCase):
    """The live pair harness's pass/fail checks offline: judge, the Executor steps and the RESTART attribution gate."""

    setUp = PairHarnessTests.setUp
    pair = PairHarnessTests.pair

    def lab(self, faults, edges, **health):
        from tests.support.pair_lab import PairLab
        lab = PairLab(self.enterContext(tempfile.TemporaryDirectory()), faults, edges=edges)
        self.addCleanup(lab.close)
        lab.health.update(health)
        return lab

    def run_lab(self, lab, **advance_options):
        from experiments.boutique.harness.multi_fault import PairRunner, replan
        from services.coordinator.core import handle
        from services.planner.core import mock_provider, retrieve_runbooks
        ids = lab.open()

        def again(name, incident_id, attempt):
            live = lab.cluster.read(lab.incidents[name]["target"])["deployment"]
            proposal = replan(lab.incidents[name], baselines=[lab.baselines[name]], retriever=retrieve_runbooks,
                              provider=mock_provider, live_template=live["spec"]["template"],
                              resource_version=live["metadata"]["resourceVersion"], attempt=attempt)
            handle(proposal, lab.store, lab.bus)
            return proposal

        runner = PairRunner(lab.store, ids, advance=lambda name, incident_id: lab.advance(
            name, incident_id, **advance_options), replan=again, clock=lambda: lab.now.timestamp(), sleep=lab.tick)
        return ids, runner.run()

    def independent(self, **advance_options):
        lab = self.lab({"shippingservice": "config", "cartservice": "pod"},
                       [("frontend", "shippingservice"), ("frontend", "cartservice")])
        return (lab, *self.run_lab(lab, **advance_options))

    def dependent(self, *, cart_ready):
        lab = self.lab({"redis-cart": "pod", "cartservice": "config"},
                       [("frontend", "cartservice"), ("cartservice", "redis-cart")], cartservice=cart_ready)
        return (lab, *self.run_lab(lab))

    def judge(self, pair_id, lab, ids, run, *, writes=None, journeys=None):
        """PairBoutiqueExperiment.judge over the lab's real Coordinator journal and Executor ledger."""
        from experiments.boutique.harness import multi_fault
        experiment = self.pair(pair_id)
        experiment.store = lab.store
        experiment.scores = Mock(return_value={})
        experiment.shop_journeys = Mock(return_value=[{"passed": True}] * 3 if journeys is None else journeys)
        counted = multi_fault.writes_by_target(lab.ledger.db) if writes is None else writes
        with patch.object(multi_fault, "writes_by_target", return_value=counted), redirect_stdout(StringIO()):
            evidence = experiment.judge({name: {"incident_id": value} for name, value in ids.items()}, run)
        return experiment, evidence

    def test_judge_passes_a_clean_independent_pair(self):
        lab, ids, run = self.independent()
        experiment, evidence = self.judge("shipping-config+cart-pod", lab, ids, run)
        self.assertEqual((evidence["result"]["result"], evidence["result"]["writes"]),
                         ("RECOVERED", {"shippingservice": 1, "cartservice": 1}))
        self.assertIsNone(evidence["result"]["ordered_recovery_shown"])
        self.assertEqual(evidence["cross_incident_transitions"], [])
        self.assertTrue((experiment.directory / "pair-result.json").is_file())

    def test_judge_fails_on_a_forged_cross_incident_journal_row(self):
        lab, ids, run = self.independent()
        shipping_message = next(key for key, payload in lab.store.db.execute("SELECT event_key, payload FROM messages")
                                if json.loads(payload).get("incident_id") == ids["shippingservice"])
        with lab.store.db:
            lab.store.db.execute("INSERT INTO transitions(incident_id, action_id, from_state, to_state, cause, "
                                 "recorded_at) VALUES (?, NULL, 'RECOVERED', 'RECOVERED', ?, ?)",
                                 (ids["cartservice"], shipping_message, "2026-10-10T12:30:00Z"))
        with self.assertRaisesRegex(RuntimeError, "changed state because of the other incident"):
            self.judge("shipping-config+cart-pod", lab, ids, run)

    def test_judge_fails_a_result_that_is_neither_expected_nor_acceptable(self):
        lab, ids, run = self.independent(verify_health={"cartservice": False})
        with self.assertRaisesRegex(RuntimeError, "ended PARTIAL; expected RECOVERED"):
            self.judge("shipping-config+cart-pod", lab, ids, run)
        lab, ids, run = self.independent()
        for writes in ({"shippingservice": 2, "cartservice": 1}, {"shippingservice": 0, "cartservice": 1},
                       {"cartservice": 1}):
            with self.subTest(writes=writes), self.assertRaisesRegex(RuntimeError, "ended UNEXPECTED_WRITES"):
                self.judge("shipping-config+cart-pod", lab, ids, run, writes=writes)

    def test_judge_scores_an_incident_the_runner_stopped_as_not_recovered(self):
        from experiments.boutique.harness.multi_fault import ApprovalRefused

        def approve(proposal, decision):
            if proposal["target"]["name"] == "cartservice":
                raise ApprovalRefused(proposal["action_id"])
            return lab.approve(proposal, decision)
        lab = self.lab({"shippingservice": "config", "cartservice": "pod"},
                       [("frontend", "shippingservice"), ("frontend", "cartservice")])
        ids, run = self.run_lab(lab, approve=approve)
        self.assertEqual((run["stops"], run["states"]["cartservice"]), ({"cartservice": "APPROVAL_REFUSED"},
                                                                        "AWAITING_APPROVAL"))
        with self.assertRaisesRegex(RuntimeError, "ended PARTIAL"):
            self.judge("shipping-config+cart-pod", lab, ids, run)
        [saved] = self.directory.glob("*/pair-result.json")
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["shop"]["not_recovered"], ["cartservice"])

    def test_judge_fails_a_recovered_dependency_pair_whose_cart_was_allowed_before_redis_recovered(self):
        lab, ids, run = self.dependent(cart_ready=True)
        _, evidence = self.judge("redis-pod+cart-config", lab, ids, run)
        self.assertEqual(evidence["result"]["result"], "RECOVERED")
        self.assertIs(evidence["result"]["ordered_recovery_shown"], True)
        cart_action = lab.store.actions_for_incident(ids["cartservice"])[-1]["action_id"]
        with lab.store.db:  # An early READY of the cart's own action, before Redis RECOVERED.
            lab.store.db.execute("INSERT INTO transitions(seq, incident_id, action_id, from_state, to_state, cause, "
                                 "recorded_at) VALUES (0, ?, ?, 'VALIDATING', 'READY', NULL, ?)",
                                 (ids["cartservice"], cart_action, "2026-10-10T12:00:36Z"))
        with self.assertRaisesRegex(RuntimeError, "allowed before the first RECOVERED"):
            self.judge("redis-pod+cart-config", lab, ids, run)

    def test_judge_accepts_a_deadlock_as_a_safe_stop_but_not_as_an_ordered_recovery(self):
        lab, ids, run = self.dependent(cart_ready=False)  # The faulted cart is NotReady, as in the live C1 runs.
        experiment, evidence = self.judge("redis-pod+cart-config", lab, ids, run, journeys=[])
        graded = evidence["result"]
        self.assertEqual((graded["result"], graded["acceptable"], graded["matched_expected"]),
                         ("DEPENDENCY_DEADLOCK", True, False))
        self.assertEqual(graded["writes"], {"redis-cart": 0, "cartservice": 0})
        self.assertIs(graded["ordered_recovery_shown"], False)
        self.assertFalse(evidence["ordering"]["passed"])
        self.assertIs(experiment.summary["pair_result"]["result"]["ordered_recovery_shown"], False)

    def test_run_pair_says_when_a_dependency_pair_showed_no_ordered_recovery(self):
        from experiments.boutique.harness import multi_fault
        from experiments.boutique.harness.multi_fault import pair_result, shop_outcome
        from experiments.boutique.harness import scenarios
        pair = scenarios.pair("redis-pod+cart-config")
        stops = {"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"}
        deadlock = pair_result(pair, shop=shop_outcome({"redis-cart": "REJECTED", "cartservice": "REJECTED"}, []),
                               stops=stops, writes={})
        ordered = pair_result(pair, shop=shop_outcome({"redis-cart": "RECOVERED", "cartservice": "RECOVERED"},
                                                      [{"passed": True}] * 3),
                              stops={}, writes={"redis-cart": 1, "cartservice": 1}, ordering={"passed": True})

        class Finished:
            def __init__(self, directory, **options):
                self.directory, self.summary = Path(directory), {"provenance": {}}

            def configure_approval(self, mode, operator):
                pass

            def run(self):
                self.summary["pair_result"] = {"result": graded}

            def close(self):
                pass
        args = SimpleNamespace(pair=pair["id"], storefront_port=8080, executor_kill=False, approval="auto-lab",
                               operator=None)
        for graded, noted in ((deadlock, True), (ordered, False)):
            output = StringIO()
            with self.subTest(result=graded["result"]), patch.object(multi_fault, "PairBoutiqueExperiment", Finished), \
                    redirect_stdout(output):
                self.assertEqual(multi_fault.run_pair(args, self.directory / graded["result"], tools={}), 0)
            self.assertIs("NOTE: DEPENDENCY_DEADLOCK is not an ordered recovery" in output.getvalue(), noted)

    # The Executor steps.

    def lane_adapters(self, writes=0):
        return SimpleNamespace(cluster=SimpleNamespace(stats={"write_attempts": writes}), ledger=Mock())

    def executing(self, experiment, action_id):
        experiment.store = Mock()
        experiment.store.pending_routes.return_value = [(1,)]
        experiment.store.route.return_value = {"action_id": action_id}
        experiment.store.approval.return_value = {"action_id": action_id}
        experiment.dependency_result = Mock(return_value={"status": "CLEAR"})
        adapters = self.lane_adapters()
        experiment.adapters = Mock(return_value=adapters)

        def apply(store, bus, *, adapters):
            adapters.cluster.stats["write_attempts"] += 1
            return [{"action_id": action_id, "status": "APPLIED"}]
        return adapters, apply

    def test_execute_lane_requires_a_changed_payload_under_the_same_action_id_to_be_refused_without_a_write(self):
        from experiments.boutique.harness import multi_fault
        from services.executor.store import ActionConflict
        proposal = {"action_id": "ACT-SHIP", "target": {"name": "shippingservice"}, "rationale": "restore"}

        def repeat_write(changed, adapters):
            adapters.cluster.stats["write_attempts"] += 1
            raise ActionConflict("reused")
        for execute, error in ((Mock(side_effect=ActionConflict("reused")), None),
                               (Mock(return_value={"status": "APPLIED"}), "Changed intent .* was accepted"),
                               (repeat_write, "A changed action ID repeated a write")):
            experiment = self.pair("shipping-config+cart-pod")
            adapters, apply = self.executing(experiment, "ACT-SHIP")
            with self.subTest(error=error), experiment.acting_as("shippingservice") as lane, \
                    patch.object(multi_fault, "process_executor_requests", side_effect=apply), \
                    patch.object(multi_fault, "execute", side_effect=execute):
                if error is None:
                    experiment.execute_lane(proposal)
                    self.assertTrue(lane["changed_payload_refused"])
                    self.assertEqual(lane["write_attempts"], 1)
                else:
                    with self.assertRaisesRegex(RuntimeError, error):
                        experiment.execute_lane(proposal)
                    self.assertNotIn("changed_payload_refused", lane)
                adapters.ledger.close.assert_called_once()

    def test_execute_lane_kills_only_the_first_repair_of_the_configuration_target(self):
        from experiments.boutique.harness import multi_fault
        from services.executor.store import ActionConflict
        experiment = self.pair("shipping-config+cart-pod", executor_kill=True)
        adapters, apply = self.executing(experiment, "ACT-SHIP")
        experiment.kill_and_reconcile = Mock()
        proposal = {"action_id": "ACT-SHIP", "target": {"name": "shippingservice"}, "rationale": "restore"}
        with experiment.acting_as("shippingservice"), \
                patch.object(multi_fault, "process_executor_requests", side_effect=apply) as process, \
                patch.object(multi_fault, "execute", side_effect=ActionConflict("reused")):
            experiment.execute_lane(proposal)
            experiment.kill_and_reconcile.assert_called_once()
            process.assert_not_called()
            experiment.killed = True
            experiment.execute_lane(proposal)
            process.assert_called_once()

    def killing(self, *, returncode=73, phase="WRITE_STARTED", status="APPLIED", reconcile_writes=0, receipt_writes=1):
        from experiments.boutique.harness import multi_fault
        experiment = self.pair("shipping-config+cart-pod", executor_kill=True)
        experiment.store = Mock()
        authority = Mock()
        authority.executor_environment.return_value = {"SENTINEL_TEST_TOKEN": "offline"}
        experiment.approval_authority = Mock(return_value=authority)
        adapters = self.lane_adapters(reconcile_writes)
        experiment.adapters = Mock(return_value=adapters)
        folder = experiment.directory / "pair-shippingservice"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "write-receipt.json").write_text(json.dumps({"stats": {"write_attempts": receipt_writes}}),
                                                   encoding="utf-8")
        ledger = Mock()
        ledger.__enter__ = Mock(return_value=ledger)
        ledger.__exit__ = Mock(return_value=False)
        ledger.get.return_value = {"phase": phase}
        proposal = {"action_id": "ACT-SHIP", "target": {"name": "shippingservice"}}
        with experiment.acting_as("shippingservice") as lane, \
                patch.object(multi_fault.subprocess, "run", return_value=SimpleNamespace(returncode=returncode)), \
                patch.object(multi_fault, "SQLiteExecutionLedger", return_value=ledger), \
                patch.object(multi_fault, "process_executor_requests",
                             return_value=[{"action_id": "ACT-SHIP", "status": status}]), \
                redirect_stdout(StringIO()):
            experiment.kill_and_reconcile(folder, proposal)
        return experiment, lane, adapters

    def test_kill_and_reconcile_records_one_write_and_a_reconciliation_without_another(self):
        experiment, lane, adapters = self.killing()
        adapters.ledger.close.assert_called_once()
        self.assertEqual(experiment.adapters.call_args.kwargs, {"coordinator": True, "reconcile_only": True})
        self.assertEqual(experiment.summary["executor_kill"]["reconcile_writes"], 0)
        self.assertEqual(lane["write_attempts"], 1)
        self.assertTrue((experiment.directory / "pair-shippingservice" / "reconciliation.json").is_file())

    def test_kill_and_reconcile_fails_the_run_on_any_other_boundary_or_write(self):
        for options, error in (({"returncode": 0}, "did not stop at the intended post-write boundary"),
                               ({"returncode": 1}, "did not stop at the intended post-write boundary"),
                               ({"phase": "COMPLETED"}, "completed its ledger entry unexpectedly"),
                               ({"status": "FAILED"}, "did not find the killed worker's committed write"),
                               ({"reconcile_writes": 1}, "Reconciliation issued another write"),
                               ({"receipt_writes": 2}, "issued more than one write")):
            with self.subTest(**options), self.assertRaisesRegex(RuntimeError, error):
                self.killing(**options)

    # Role 6: only a recovered chaos RESTART is attributed.

    def verifying(self, name, proposal, outcomes):
        from experiments.boutique.harness import multi_fault
        experiment = self.pair()
        experiment.kubectl = Mock()
        experiment.attribute = Mock()
        ledger = Mock()
        ledger.get.return_value = None
        with experiment.acting_as(name), patch.object(multi_fault, "VerificationStore", return_value=ledger), \
                patch.object(multi_fault, "process_verifier_requests", return_value=outcomes):
            experiment.verify_lane(proposal, {"action_id": proposal["action_id"]})
        ledger.close.assert_called_once()
        return experiment.attribute

    def test_verify_lane_attributes_only_a_recovered_restart_of_the_chaos_target(self):
        def outcome(name, action_id, status):
            return {"target": {"name": name}, "action_id": action_id, "status": status}
        restart = {"action_id": "ACT-REDIS", "action": "RESTART", "target": {"name": "redis-cart"}}
        rollback = {"action_id": "ACT-CART", "action": "ROLLBACK", "target": {"name": "cartservice"}}
        self.verifying("redis-cart", restart, [outcome("redis-cart", "ACT-REDIS", "RECOVERED")]).assert_called_once()
        for name, proposal, outcomes in (
                ("redis-cart", restart, [outcome("redis-cart", "ACT-REDIS", "NOT_RECOVERED")]),
                ("redis-cart", restart, [outcome("cartservice", "ACT-CART", "RECOVERED")]),
                ("redis-cart", restart, []),
                ("redis-cart", dict(restart, action="ROLLBACK"), [outcome("redis-cart", "ACT-REDIS", "RECOVERED")]),
                ("cartservice", rollback, [outcome("cartservice", "ACT-CART", "RECOVERED")]),
                ("cartservice", dict(rollback, action="RESTART"), [outcome("cartservice", "ACT-CART", "RECOVERED")])):
            with self.subTest(name=name, action=proposal["action"], outcomes=outcomes):
                self.verifying(name, proposal, outcomes).assert_not_called()


if __name__ == "__main__":
    unittest.main()
