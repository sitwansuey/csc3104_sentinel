"""Offline connected-workload checks; HTTP services and Kubernetes are fake."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
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


if __name__ == "__main__":
    unittest.main()
