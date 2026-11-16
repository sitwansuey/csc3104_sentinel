"""The offline partial assessor shares the full Guard's state/policy checks."""

from copy import deepcopy
from datetime import timedelta
import unittest

from experiments.validation.demo_guard_v2 import build_inputs
from services.guard.core import GuardInputError, assess_state_policy, evaluate_with_report


STATE_POLICY_REASONS = {
    "TARGET_NOT_ALLOWED", "ACTION_NOT_ALLOWED", "ACTION_NOT_IMPLEMENTED",
    "LIVE_TARGET_MISMATCH", "TARGET_UID_MISMATCH", "STALE_RESOURCE_VERSION",
    "LIVE_STATE_FROM_FUTURE", "STALE_LIVE_STATE",
}


class GuardStatePolicyTests(unittest.TestCase):
    def assess(self, values):
        return assess_state_policy(values["proposal"], values["live_state"],
                                   values["policy"], now=values["now"])

    def test_checks_match_full_audit_without_mutating_inputs(self):
        cases = [build_inputs() for _ in range(10)]
        cases[1]["policy"]["targets"] = []
        cases[2]["policy"]["allowed_actions"] = []
        cases[3]["proposal"].update(action="NO_ACTION", parameters={})
        cases[3]["policy"]["allowed_actions"].append("NO_ACTION")
        cases[4]["live_state"]["cluster_id"] = "another-cluster"
        cases[5]["live_state"]["deployment"]["metadata"]["uid"] = "another-uid"
        cases[6]["live_state"]["deployment"]["metadata"]["resourceVersion"] = "103"
        cases[7]["live_state"]["captured_at"] = (cases[7]["now"] + timedelta(seconds=1)).isoformat()
        cases[8]["live_state"]["captured_at"] = (cases[8]["now"] - timedelta(seconds=16)).isoformat()
        cases[9]["policy"]["targets"] = []
        cases[9]["live_state"]["deployment"]["metadata"]["resourceVersion"] = "103"
        for index, values in enumerate(cases):
            with self.subTest(case=index):
                original = deepcopy(values)
                reasons = self.assess(values)
                _, report = evaluate_with_report(**values)
                full_reasons = [item["code"] for item in report["checks"]
                                if item["status"] == "FAIL"
                                and item["code"] in STATE_POLICY_REASONS]
                self.assertEqual(reasons, full_reasons)
                self.assertEqual(values, original)

    def test_passing_partial_assessment_does_not_imply_full_allow(self):
        values = build_inputs()
        values["dependencies"].update(status="BLOCKED", blockers=["Dependency is unhealthy"])
        values["approval"] = None
        self.assertEqual(self.assess(values), [])
        decision, _ = evaluate_with_report(**values)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("DEPENDENCY_BLOCKED", decision["reason_codes"])

    def test_state_age_boundary_uses_same_inclusive_rule(self):
        values = build_inputs()
        values["live_state"]["captured_at"] = (values["now"] - timedelta(seconds=15)).isoformat()
        self.assertEqual(self.assess(values), [])
        values["now"] += timedelta(microseconds=1)
        self.assertEqual(self.assess(values), ["STALE_LIVE_STATE"])

    def test_invalid_inputs_raise_instead_of_passing_partial_assessment(self):
        for field in ("resourceVersion", "uid"):
            with self.subTest(field=field):
                values = build_inputs()
                del values["live_state"]["deployment"]["metadata"][field]
                with self.assertRaises(GuardInputError):
                    self.assess(values)
        values = build_inputs()
        values["now"] = values["now"].replace(tzinfo=None)
        with self.assertRaises(GuardInputError):
            self.assess(values)


if __name__ == "__main__":
    unittest.main()
