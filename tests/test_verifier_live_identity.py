import unittest
from services.guard.common import json_sha256
from services.verifier.live import LiveEvidenceCollector
from tests.test_verifier_live import configuration, Kubernetes, HTTP, NOW

class CompatibilityTests(unittest.TestCase):
    def test_live_reading_retains_template_availability_and_source_identity(self):
        config, kubernetes = configuration(), Kubernetes()
        config.update(dependency_inventory_complete=True, declared_edges=[{"from": "checkout", "to": "payment"}])
        for deployment in kubernetes.resources.values():
            deployment["spec"]["template"] = {"metadata": {"labels": {"app": "test"}}}
        collector = LiveEvidenceCollector(config, kubernetes, http=HTTP(), clock=lambda: NOW)
        sample = collector.samples({"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-test",
            "action_id": "ACT-test", "target": config["services"]["checkout"]["target"], "status": "APPLIED",
            "resource_version": "opaque", "recorded_at": NOW.isoformat(), "details": "fixture"})[0]
        self.assertTrue(sample["configuration_stable"])
        self.assertEqual(sample["available_replicas"], 1)
        self.assertEqual(sample["template_sha256"], json_sha256(kubernetes.resources["checkout"]["spec"]["template"]))
        self.assertEqual(sample["observations"]["payment"]["target"]["uid"], "payment-uid")
        self.assertEqual(sample["dependency_evidence"][0]["source"], "declared_configuration")
