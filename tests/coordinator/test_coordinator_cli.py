"""Exercise the persistent CLI across separate processes."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class CoordinatorCLITests(unittest.TestCase):
    def test_guard_command_processes_persisted_requests_using_real_evaluator(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "coordinator.db")
            state_path = Path(directory) / "state.json"
            observations = json.loads((ROOT / "data/sentinel/samples/observations.json").read_text())
            state_path.write_text(json.dumps(observations["failed-rollout"][-1]))
            def run(*args):
                result = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "scripts/cli/coordinator.py"), "--db", database, *args],
                    capture_output=True, text=True, cwd=directory, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return [json.loads(line) for line in result.stdout.splitlines()]
            run("ingest", str(ROOT / "data/sentinel/samples/messages/incident.json"))
            run("ingest", str(ROOT / "data/sentinel/samples/messages/proposal.json"))
            output = run("guard", "--policy", str(ROOT / "data/sentinel/samples/policy.json"),
                         "--baseline", str(ROOT / "data/sentinel/samples/baseline.json"),
                         "--dependencies", str(ROOT / "data/sentinel/samples/messages/dependency-result.json"),
                         "--state", str(state_path), "--now", "2026-10-10T12:00:35Z")
            self.assertEqual(output[0]["guard_result"]["verdict"], "ESCALATE")
            self.assertEqual(output[0]["guard_result"]["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
            self.assertEqual(run("status", "INC-DEMO-001")[0]["state"], "AWAITING_APPROVAL")
            self.assertFalse(any(item.get("topic") == "executor.request" for item in output))

    def test_ingest_approve_restart_replay_and_acknowledge(self):
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "nested" / "coordinator.db")
            def run(*args):
                result = subprocess.run(
                    [sys.executable, "-B", str(ROOT / "scripts/cli/coordinator.py"), "--db", database, *args],
                    capture_output=True, text=True, cwd=directory, timeout=15,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return [json.loads(line) for line in result.stdout.splitlines()]
            run("ingest", str(ROOT / "data/sentinel/samples/messages/incident.json"))
            run("ingest", str(ROOT / "data/sentinel/samples/messages/proposal.json"))
            run("ingest", str(ROOT / "data/sentinel/samples/messages/guard-result.json"))
            expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
            output = run("approve", "ACT-DEMO-001", "cli-operator", "--expires-at", expiry)
            self.assertEqual(output[0]["approval"]["operator_id"], "cli-operator")
            self.assertEqual(output[0]["approval"]["expires_at"], expiry)
            status = run("status", "INC-DEMO-001")[0]
            self.assertEqual(status["state"], "VALIDATING")
            self.assertIsInstance(status["target"], dict)
            pending = run("pending")
            self.assertTrue(any(e["payload"].get("approval") for e in pending))
            for event in pending:
                run("ack", str(event["event_id"]))
            self.assertEqual(run("pending"), [])


if __name__ == "__main__":
    unittest.main()
