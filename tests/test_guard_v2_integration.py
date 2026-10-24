"""Full fixture -> CLI/report -> bound-consumer Guard integration."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.demo_guard import DEFAULT_NOW
from scripts.demo_guard_v2 import SCENARIOS, build_inputs, export_inputs, run_scenario
from services.guard import GuardInputError, assert_decision_usable, evaluate, evaluate_with_report
from shared.contracts import validate_message


ROOT = Path(__file__).resolve().parents[1]


class GuardV2IntegrationTests(unittest.TestCase):
    def test_all_enhanced_scenarios_hit_the_intended_rule(self):
        for name, (verdict, reason) in SCENARIOS.items():
            with self.subTest(scenario=name):
                result = run_scenario(name)
                decision = result["decision"]
                validate_message(decision)
                self.assertEqual(decision["schema_version"], "2.0")
                self.assertEqual(decision["verdict"], verdict)
                self.assertIn(reason, decision["reason_codes"])
                self.assertEqual(result["report"]["verdict"], verdict)

    def test_exported_fixture_runs_through_actual_cli_with_private_report(self):
        with tempfile.TemporaryDirectory() as directory:
            export = Path(directory) / "inputs"
            export_inputs(export, ["allowed"])
            location = export / "allowed"
            report_path = Path(directory) / "audit.json"
            arguments = [sys.executable, "-m", "services.guard_cli"]
            for name in ("proposal", "policy", "baseline", "dependencies", "approval", "live-state", "preview"):
                arguments.extend([f"--{name}", str(location / f"{name}.json")])
            arguments.extend(["--now", DEFAULT_NOW, "--report", str(report_path)])
            completed = subprocess.run(arguments, cwd=ROOT, capture_output=True, text=True,
                                       encoding="utf-8", timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stderr, "")
            decision = json.loads(completed.stdout)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(decision["inputs_sha256"], report["inputs_sha256"])
            self.assertTrue(assert_decision_usable(decision, **build_inputs()))
            self.assertTrue(any(item["code"] == "SERVER_DRY_RUN_PASSED" for item in report["checks"]))

    def test_live_profile_will_not_issue_an_already_expired_allow(self):
        values = build_inputs()
        values["live_state"]["captured_at"] = (
            values["now"] - timedelta(seconds=values["policy"]["max_state_age_seconds"])
        ).isoformat()
        result = evaluate(**values)
        self.assertEqual(result["verdict"], "ESCALATE")
        self.assertIn("DECISION_WINDOW_EXHAUSTED", result["reason_codes"])

    def test_a_consumer_cannot_extend_evidence_or_policy_deadline(self):
        values = build_inputs()
        result = evaluate(**values)
        result["valid_until"] = (values["now"] + timedelta(minutes=1)).isoformat()
        with self.assertRaises(GuardInputError):
            assert_decision_usable(result, **values)

    def test_huge_duration_policies_are_errors_before_deadline_arithmetic(self):
        for key in ("max_state_age_seconds", "max_dependency_age_seconds", "max_approval_lifetime_seconds"):
            with self.subTest(key=key):
                values = build_inputs()
                values["policy"][key] = 10 ** 10000
                with self.assertRaises(GuardInputError):
                    evaluate(**values)

    def test_near_maximum_clock_is_a_safe_input_error(self):
        values = build_inputs()
        values["now"] = datetime.max.replace(tzinfo=timezone.utc)
        with self.assertRaises(GuardInputError):
            evaluate(**values)

    def test_a_hard_rejection_does_not_hide_unknown_dependencies_in_audit(self):
        values = build_inputs()
        values["policy"]["targets"] = []
        values["dependencies"]["status"] = "UNKNOWN"
        result, report = evaluate_with_report(**values)
        self.assertEqual(result["verdict"], "REJECT")
        self.assertNotIn("DEPENDENCY_UNKNOWN", result["reason_codes"])
        self.assertTrue(any(item["code"] == "DEPENDENCY_UNKNOWN" and item["status"] == "UNKNOWN"
                            for item in report["checks"]))

    def test_enhanced_evaluation_does_not_modify_producer_records(self):
        values = build_inputs()
        original = deepcopy(values)
        evaluate_with_report(**values)
        self.assertEqual(values, original)


if __name__ == "__main__":
    unittest.main()
