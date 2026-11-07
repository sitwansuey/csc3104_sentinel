from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.validation import demo_observer_live as live


ROOT = Path(__file__).resolve().parents[1]


class SequenceApi:
    def __init__(self, responses):
        self.responses = iter(responses)

    def read_namespaced_deployment(self, **kwargs):
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return copy.deepcopy(item)


class LiveCliTests(unittest.TestCase):
    def healthy_deployment(self):
        fixture = json.loads((ROOT / "data/sentinel/samples/observations.json").read_text(encoding="utf-8"))
        return fixture["healthy"][0]["deployment"]

    def test_partial_collection_returns_failure_and_retains_evidence(self):
        api = SequenceApi([self.healthy_deployment(), RuntimeError("API disconnected")])
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.json"
            with patch.object(live, "KubectlAppsApi", return_value=api), redirect_stdout(output):
                code = live.main([
                    "--poll-count", "2", "--interval-seconds", "0.001",
                    "--max-consecutive-errors", "0", "--output", str(path),
                ])
            evidence = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(evidence["observations_read"], 1)
        self.assertEqual(evidence["errors"], ["API disconnected"])
        self.assertEqual(json.loads(output.getvalue()), evidence)

    def test_short_healthy_acceptance_fails_and_saves_report(self):
        api = SequenceApi([self.healthy_deployment(), self.healthy_deployment()])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.json"
            with patch.object(live, "KubectlAppsApi", return_value=api), redirect_stdout(io.StringIO()):
                code = live.main([
                    "--poll-count", "2", "--interval-seconds", "0.001",
                    "--expect", "healthy", "--output", str(path),
                ])
            evidence = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertFalse(evidence["validation"]["passed"])
        self.assertIn("Healthy observation window is too short.", evidence["validation"]["problems"])


if __name__ == "__main__":
    unittest.main()
