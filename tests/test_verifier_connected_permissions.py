import unittest
from scripts.verifier_lab_manifest import manifest

class CompatibilityTests(unittest.TestCase):
    def test_lab_verifier_role_has_no_kubernetes_mutation_permissions(self):
        items = manifest("verifier-connected-fixture")["items"]
        role = next(item for item in items if item["kind"] == "Role" and item["metadata"]["name"] == "verifier")
        self.assertEqual(role["rules"], [{"apiGroups": ["apps"], "resources": ["deployments"],
                                        "resourceNames": ["checkout", "payment"], "verbs": ["get"]}])
