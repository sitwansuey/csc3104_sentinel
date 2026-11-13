"""Every demo_guard scenario still runs against the current message contracts.

The demo builds its proposals by hand, so a contract change (for example the
P5.07 SCALE parameters) can break it without any other test noticing.
"""

from datetime import UTC, datetime
import contextlib
import io
import json
import sys
import unittest
from unittest.mock import patch

from experiments.validation import demo_guard
from shared.contracts import validate_message


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
# Verdicts and reason codes as integration/phase4 printed them, except that the
# forbidden SCALE now also fails the SCALE necessity checks: the demo policy has
# no scale_constraints, and the failed-rollout template is not the baseline.
EXPECTED = {
    "valid-approved": ("ALLOW", ["VALIDATION_PASSED"]),
    "missing-approval": ("ESCALATE", ["OPERATOR_APPROVAL_REQUIRED"]),
    "forbidden-target": ("REJECT", ["TARGET_NOT_ALLOWED"]),
    "forbidden-action": ("REJECT", ["ACTION_NOT_ALLOWED", "SCALE_TARGET_NOT_ALLOWED", "SCALE_TEMPLATE_DRIFT"]),
    "stale-state": ("REJECT", ["STALE_RESOURCE_VERSION"]),
    "tampered-baseline": ("REJECT", ["BASELINE_HASH_MISMATCH"]),
    "blocked-dependencies": ("REJECT", ["DEPENDENCY_BLOCKED"]),
    "unknown-dependencies": ("ESCALATE", ["DEPENDENCY_UNKNOWN"]),
}


class DemoGuardScenarioTests(unittest.TestCase):
    def test_every_scenario_produces_a_valid_guard_result(self):
        self.assertEqual(set(EXPECTED), set(demo_guard.SCENARIOS))
        fixtures = demo_guard.load_fixtures()
        for name in demo_guard.SCENARIOS:
            with self.subTest(scenario=name):
                result = demo_guard.run_scenario(name, fixtures, NOW)
                validate_message(result)
                self.assertEqual(result["kind"], "guard_result")
                verdict, codes = EXPECTED[name]
                self.assertEqual((result["verdict"], result["reason_codes"]), (verdict, codes))

    def test_forbidden_action_uses_the_current_scale_shape(self):
        fixtures = demo_guard.load_fixtures()
        proposal = demo_guard.scale_proposal(fixtures)
        validate_message(proposal)
        self.assertEqual(proposal["action"], "SCALE")
        self.assertEqual(set(proposal["parameters"]), {"baseline_id", "desired_replicas", "template_sha256"})
        self.assertEqual(proposal["parameters"]["baseline_id"], fixtures["proposal"]["parameters"]["baseline_id"])
        self.assertEqual(proposal["parameters"]["desired_replicas"], 2)

    def test_main_prints_every_scenario(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["demo_guard.py"]), contextlib.redirect_stdout(output):
            demo_guard.main()
        self.assertEqual(set(json.loads(output.getvalue())), set(demo_guard.SCENARIOS))


if __name__ == "__main__":
    unittest.main()
