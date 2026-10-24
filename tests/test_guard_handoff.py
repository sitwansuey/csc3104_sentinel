"""The real Guard participates in a fake consumer/API integration flow."""

import unittest

from scripts.demo_guard import load_fixtures
from scripts.demo_guard_handoff import FakeCluster, SCENARIOS, run_scenario


class GuardHandoffTests(unittest.TestCase):
    def test_current_approved_proposal_restores_the_exact_checked_template(self):
        fixtures = load_fixtures()
        result = run_scenario("allowed", fixtures)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(result["write_attempts"], 1)
        self.assertEqual(result["writes"], 1)
        self.assertEqual(len(result["decisions"]), 2)
        self.assertEqual(result["applied_template_sha256"], fixtures["baseline"]["template_sha256"])

    def test_every_denied_or_unavailable_input_path_makes_zero_writes(self):
        for name in SCENARIOS:
            if name == "allowed":
                continue
            with self.subTest(scenario=name):
                self.assertEqual(run_scenario(name)["writes"], 0)

    def test_refused_or_invalid_requests_never_attempt_the_write_api(self):
        for name in SCENARIOS:
            if name in ("allowed", "state-changed-after-recheck"):
                continue
            with self.subTest(scenario=name):
                self.assertEqual(run_scenario(name)["write_attempts"], 0)

    def test_final_check_catches_changed_state_approval_and_dependencies(self):
        expected = {
            "state-changed-before-recheck": ("REJECT", "STALE_RESOURCE_VERSION"),
            "approval-expired-before-recheck": ("ESCALATE", "APPROVAL_EXPIRED"),
            "approval-revoked-before-recheck": ("ESCALATE", "OPERATOR_APPROVAL_REQUIRED"),
            "dependency-blocked-before-recheck": ("REJECT", "DEPENDENCY_BLOCKED"),
        }
        for name, (verdict, reason) in expected.items():
            with self.subTest(scenario=name):
                result = run_scenario(name)
                self.assertEqual(result["decisions"][0]["verdict"], "ALLOW")
                self.assertEqual(result["decisions"][1]["verdict"], verdict)
                self.assertIn(reason, result["decisions"][1]["reason_codes"])

    def test_conditional_api_catches_the_race_after_final_guard_validation(self):
        result = run_scenario("state-changed-after-recheck")
        self.assertEqual([d["verdict"] for d in result["decisions"]], ["ALLOW", "ALLOW"])
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(result["write_attempts"], 1)
        self.assertEqual(result["writes"], 0)

    def test_conditional_api_refuses_a_recreated_target_with_same_name(self):
        fixtures = load_fixtures()
        cluster = FakeCluster(fixtures["live_state"])
        cluster.snapshot["deployment"]["metadata"]["uid"] = "replacement-uid"
        self.assertFalse(cluster.conditional_restore(
            target=fixtures["proposal"]["target"],
            version=fixtures["proposal"]["based_on_resource_version"],
            template=fixtures["baseline"]["pod_template"],
        ))
        self.assertEqual(cluster.writes, [])
        self.assertEqual(len(cluster.write_attempts), 1)


if __name__ == "__main__":
    unittest.main()
