import json
import unittest
from pathlib import Path

from services.verifier import verify
from shared.contracts import validate_message

ROOT = Path(__file__).resolve().parents[1]


def load_sample(name):
    return json.loads((ROOT / "samples" / name).read_text())


class VerifierTests(unittest.TestCase):
    def test_stable_health_reports_recovered(self):
        result = verify(
            load_sample("messages/execution-result.json"),
            load_sample("health.json")["stable"],
            load_sample("policy.json"),
        )

        validate_message(result)
        self.assertEqual(result["kind"], "outcome")
        self.assertEqual(result["status"], "RECOVERED")
