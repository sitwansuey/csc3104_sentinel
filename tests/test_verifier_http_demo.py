import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class HTTPDemoTests(unittest.TestCase):
    def test_real_http_demo_records_expected_decisions_and_limitations(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts" / "demo_verifier_http.py")],
                                    cwd=directory, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["mode"], "real_http_simulated_kubernetes")
        expected = {"healthy": "RECOVERED", "service_failure": "NOT_RECOVERED",
                    "dependency_failure": "NOT_RECOVERED", "unknown_inventory": "UNKNOWN"}
        for scenario, status in expected.items():
            with self.subTest(scenario=scenario):
                self.assertEqual(report["results"][scenario]["status"], status)
                self.assertEqual(report["results"][scenario]["readings"], 3)
