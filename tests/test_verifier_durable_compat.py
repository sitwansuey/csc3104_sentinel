"""Durable polling preserves the merged protocol and approved-template guarantees."""

from datetime import timedelta
import unittest

from runtime.verifier_pipeline import process_verifier_requests
from services.guard_common import json_sha256
from services.verifier_live import LiveEvidenceCollector
from services.verifier_store import VerificationStore
from tests.test_verifier_live import configuration, Kubernetes, HTTP, NOW
from tests import test_verifier_pipeline as pipeline_fixtures
from scripts.verifier_lab_manifest import manifest


class DurableCompatibilityTests(unittest.TestCase):
    def test_grpc_policy_uses_persisted_approved_hash_in_durable_mode(self):
        setup = pipeline_fixtures.VerifierWorkerTests()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        ledger = VerificationStore()
        self.addCleanup(ledger.close)
        policy = pipeline_fixtures.grpc_policy("a" * 64)  # A reader cannot choose another expected template.
        outcomes = process_verifier_requests(setup.store, setup.bus, ledger=ledger,
            policy_reader=lambda execution: policy, sample_reader=lambda execution: setup.samples,
            clock=lambda: pipeline_fixtures.NOW + timedelta(seconds=20))
        self.assertEqual(outcomes[0]["status"], "RECOVERED")
        self.assertEqual(ledger.get(setup.execution["action_id"])["policy"]["health"]["expected_template_sha256"],
                         setup.proposal["parameters"]["template_sha256"])

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

    def test_lab_verifier_role_has_no_kubernetes_mutation_permissions(self):
        items = manifest("verifier-connected-fixture")["items"]
        role = next(item for item in items if item["kind"] == "Role" and item["metadata"]["name"] == "verifier")
        self.assertEqual(role["rules"], [{"apiGroups": ["apps"], "resources": ["deployments"],
                                        "resourceNames": ["checkout", "payment"], "verbs": ["get"]}])


if __name__ == "__main__":
    unittest.main()
