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

from scripts.boutique.boutique_connected import (APPLICATIONS, UPSTREAM, UPSTREAM_SHA256, ConnectedBoutiqueExperiment,
                                        configured_graph, connected_manifest, decoded_resources, image_digest, readiness)
from scripts.boutique.boutique_storefront import StorefrontProbe
from scripts.boutique.test_boutique_live import BoutiqueExperiment
from services.executor.store import fingerprint
from services.verifier.core import verify


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
