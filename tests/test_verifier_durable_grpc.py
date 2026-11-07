from datetime import timedelta
import unittest
from runtime.verifier_pipeline import process_verifier_requests
from services.verifier.store import VerificationStore
from tests import test_verifier_pipeline as pipeline_fixtures

class CompatibilityTests(unittest.TestCase):
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
