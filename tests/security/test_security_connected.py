from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tarfile
import tempfile
import unittest

from runtime.security_store import import_inspections, recent, update_alert
from scripts.security.boutique_security_manifest import CALLERS, NAMESPACE, caller_config, overlay, proxy_config
from scripts.security.scan_image_security import archive_config_id
from scripts.security.watch_security import assess_scan, inspection_alerts, load_inventory, scan_cycle
from services.security.image_security import summarize


IMAGE = "registry.example/app@sha256:" + "a" * 64
METHOD = "/hipstershop.ShippingService/GetQuote"


def base():
    return {"items": [{"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": NAMESPACE},
                       "spec": {"template": {"spec": {"containers": [{"name": "server", "env": [
                           {"name": "SHIPPING_SERVICE_ADDR", "value": "shippingservice:50051"}]}]}}}} for name in (*CALLERS,"shippingservice")]}


class ConnectedSecurityTests(unittest.TestCase):
    def test_real_callers_use_private_loopback_and_mutually_authenticated_gateway(self):
        source = base()
        result = overlay(source, envoy_image="envoy@sha256:abc", inspector_image="inspector@sha256:abc")
        self.assertEqual(source, base())
        for item in result["items"]:
            if item["kind"] == "Deployment" and item["metadata"]["name"] in CALLERS:
                pod = item["spec"]["template"]["spec"]
                self.assertEqual(pod["containers"][0]["env"][0]["value"], "127.0.0.1:50052")
                self.assertEqual(pod["containers"][1]["name"], "shipping-egress")
                self.assertNotIn("volumeMounts", pod["containers"][0])
        config = caller_config("frontend-client")
        listener = config["static_resources"]["listeners"][0]
        self.assertEqual(listener["address"]["socket_address"]["address"], "127.0.0.1")
        self.assertEqual(config["static_resources"]["clusters"][0]["transport_socket"]["typed_config"]["sni"], "gateway.internal")

    def test_policies_allow_only_gateway_upstream_backend_chain(self):
        result = overlay(base(), envoy_image="envoy", inspector_image="inspector")
        policies = {i["spec"]["podSelector"]["matchLabels"]["app"]: i["spec"] for i in result["items"] if i["kind"] == "NetworkPolicy"}
        for target, caller, port in (("shippingservice", "security-gateway", 8444),):
            self.assertEqual(policies[target]["ingress"], [{"from": [{"podSelector": {"matchLabels": {"app": caller}}}],
                                                         "ports": [{"protocol": "TCP", "port": port}]}])
        self.assertEqual(len(policies), 2)
        cluster = proxy_config(True)["static_resources"]["clusters"][0]
        self.assertEqual(cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][0]["endpoint"]["address"]["socket_address"]["address"],"127.0.0.1")

    def test_tls_server_checks_peer_identity_in_addition_to_ca(self):
        for upstream, names in ((False, {"frontend-client.internal", "checkoutservice-client.internal", "client.internal"}),
                                (True, {"proxy-client.internal"})):
            config = proxy_config(upstream)
            tls = config["static_resources"]["listeners"][0]["filter_chains"][0]["transport_socket"]["typed_config"]
            self.assertTrue(tls["require_client_certificate"])
            sans = tls["common_tls_context"]["validation_context"]["match_typed_subject_alt_names"]
            self.assertEqual({san["matcher"]["exact"] for san in sans}, names)

    def test_changed_upstream_caller_configuration_requires_review(self):
        source = base()
        source["items"][0]["spec"]["template"]["spec"]["containers"][0]["env"] = []
        with self.assertRaises(ValueError):
            overlay(source, envoy_image="envoy", inspector_image="inspector")


class ArchiveBindingTests(unittest.TestCase):
    def archive(self, folder, *, tamper=False):
        config = json.dumps({"os": "linux", "architecture": "amd64"}).encode()
        config_id = "sha256:" + hashlib.sha256(config).hexdigest()
        manifest = json.dumps({"config": {"digest": config_id}}).encode()
        manifest_id = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"manifests": [{"digest": manifest_id, "platform": {"os": "linux", "architecture": "amd64"}}]}).encode()
        image_id = "sha256:" + hashlib.sha256(index).hexdigest()
        files = {"manifest.json": json.dumps([{"Config": "blobs/sha256/" + config_id.split(":")[1]}]).encode(),
                 "blobs/sha256/" + config_id.split(":")[1]: config,
                 "blobs/sha256/" + manifest_id.split(":")[1]: manifest,
                 "blobs/sha256/" + image_id.split(":")[1]: index + (b" " if tamper else b"")}
        path = Path(folder) / "image.tar"
        with tarfile.open(path, "w") as archive:
            for name, raw in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
        return path, image_id, config_id

    def test_oci_index_and_platform_config_are_distinct_but_cryptographically_bound(self):
        with tempfile.TemporaryDirectory() as folder:
            path, image, config = self.archive(folder)
            self.assertNotEqual(image, config)
            self.assertEqual(archive_config_id(path, image), config)

    def test_tampered_descriptor_chain_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path, image, _ = self.archive(folder, tamper=True)
            with self.assertRaises(ValueError):
                archive_config_id(path, image)


class SecurityMonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.journal = self.root / "telemetry.sqlite3"

    def events(self):
        with closing(sqlite3.connect(self.journal)) as db:
            return recent(db)

    def test_duplicate_imports_are_suppressed_and_raw_extra_fields_refused(self):
        event = {"kind": "inspection", "at": "2026-11-04T01:00:00Z", "method": METHOD, "bytes": 20, "verdict": "DENY", "rule": "QUANTITY"}
        import_inspections(self.journal, [event, event])
        import_inspections(self.journal, [event])
        self.assertEqual(len(self.events()), 1)
        with self.assertRaises(ValueError):
            import_inspections(self.journal, [{**event, "raw_payload": "PRIVATE"}])

    def test_alerts_deduplicate_across_restarts_and_resolve_on_verified_clean_scan(self):
        report = {"image": IMAGE, "status": "FINDINGS", "counts": {"HIGH": 1, "CRITICAL": 0},
                  "findings": [{"id": "CVE-TEST", "package": "lib", "installed": "1", "severity": "HIGH"}]}
        assess_scan(self.journal, report)
        assess_scan(self.journal, report)
        self.assertEqual(len(self.events()), 1)
        assess_scan(self.journal, {"image": IMAGE, "status": "UNKNOWN"})
        self.assertEqual(len(self.events()), 2)
        self.assertTrue(all(e["status"] == "OPEN" for e in self.events()))
        assess_scan(self.journal, {"image": IMAGE, "status": "NO_KNOWN_CVES", "counts": {}, "findings": []})
        self.assertEqual(sum(e["status"] == "RESOLVED" for e in self.events()), 2)

    def test_recent_denials_alert_but_old_and_future_records_do_not(self):
        now = datetime(2026,11,4,1,tzinfo=timezone.utc)
        event = {"kind": "inspection", "at": now.isoformat(), "method": METHOD, "bytes": 20, "verdict": "DENY", "rule": "QUANTITY"}
        import_inspections(self.journal, [event])
        inspection_alerts(self.journal, now=now)
        inspection_alerts(self.journal, now=now)
        self.assertEqual(sum(e["kind"] == "alert" for e in self.events()), 1)
        inspection_alerts(self.journal, now=now+timedelta(seconds=61))
        self.assertEqual(self.events()[0]["status"], "RESOLVED")

    def test_scan_error_does_not_stop_remaining_images_or_create_clean_evidence(self):
        calls = []
        def scanner(image, directory, **kwargs):
            calls.append(image)
            raise RuntimeError("network unavailable")
        inventory = [{"image": IMAGE}, {"image": IMAGE.replace("a"*64,"b"*64)}]
        results = scan_cycle(inventory, self.journal, self.root / "scans", scanner=scanner)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r["status"] == "UNKNOWN" for r in results))
        self.assertEqual(sum(e["kind"] == "alert" for e in self.events()), 2)

    def test_inventory_rejects_mutable_tags_extra_actions_and_duplicates(self):
        path = self.root / "inventory.json"
        for value in ([{"image": "app:latest"}], [{"image": IMAGE, "command": "patch"}], [{"image": IMAGE}]*2, []):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                load_inventory(path)


if __name__ == "__main__":
    unittest.main()
