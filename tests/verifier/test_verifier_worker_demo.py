import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class WorkerDemoTests(unittest.TestCase):
    def test_documented_command_works_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demos" / "demo_verifier_worker.py")],
                                    cwd=directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "synthetic_offline")
        expected = {"healthy": "RECOVERED", "unhealthy": "NOT_RECOVERED",
                    "timeout": "UNKNOWN", "restart": "RECOVERED"}
        for name, status in expected.items():
            self.assertEqual(report["results"][name]["outcome"], status)
            self.assertTrue(report["results"][name]["outcome_delivered"])
