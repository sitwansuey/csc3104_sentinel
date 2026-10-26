from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from services.verifier_cli import main
from tests.test_verifier_store import fixture
from tests.test_verifier_worker import ready_store


class VerifierCLITests(unittest.TestCase):
    def test_file_runner_delivers_outcome_and_is_safe_to_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = ready_store(root / "coordinator.sqlite3")
            coordinator.close()
            (root / "policy.json").write_text(json.dumps(fixture("policy.json")), encoding="utf-8")
            (root / "samples.json").write_text(json.dumps(fixture("health.json")["stable"]), encoding="utf-8")
            args = ["--coordinator-db", str(root / "coordinator.sqlite3"), "--state", str(root / "verifier.sqlite3"),
                    "--policy", str(root / "policy.json"), "--samples", str(root / "samples.json"),
                    "--now", "2026-10-10T12:01:10Z"]
            first, second = io.StringIO(), io.StringIO()
            with redirect_stdout(first):
                self.assertEqual(main(args), 0)
            with redirect_stdout(second):
                self.assertEqual(main(args), 0)
            self.assertEqual(json.loads(first.getvalue())["outcomes"][0]["status"], "RECOVERED")
            self.assertEqual(json.loads(second.getvalue())["outcomes"], [])

    def test_missing_coordinator_is_reported_without_creating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.sqlite3"
            errors = io.StringIO()
            with redirect_stderr(errors):
                result = main(["--coordinator-db", str(path), "--state", str(Path(directory) / "state.sqlite3"),
                               "--policy", "unused", "--samples", "unused"])
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(errors.getvalue())["kind"], "verifier_error")
            self.assertFalse(path.exists())

    def test_input_failure_leaves_coordinator_verifying(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coordinator = ready_store(root / "coordinator.sqlite3")
            try:
                (root / "policy.json").write_text("invalid json", encoding="utf-8")
                with redirect_stderr(io.StringIO()):
                    result = main(["--coordinator-db", str(root / "coordinator.sqlite3"),
                                   "--state", str(root / "state.sqlite3"), "--policy", str(root / "policy.json"),
                                   "--samples", "unused", "--now", "2026-10-10T12:01:10Z"])
                self.assertEqual(result, 1)
                self.assertEqual(coordinator.action("ACT-DEMO-001")["state"], "VERIFYING")
            finally:
                coordinator.close()
