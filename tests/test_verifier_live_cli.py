from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.verifier_live_cli import main
from tests.test_verifier_live import configuration, HTTP, Kubernetes


class LiveCLITests(unittest.TestCase):
    def test_sample_uses_explicit_context_and_configured_inventory(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "inventory.json"
            config.write_text(json.dumps(configuration()), encoding="utf-8")
            output = io.StringIO()
            with patch("services.verifier_live_cli.KubectlReader", return_value=Kubernetes()) as reader:
                with patch("services.verifier_live.EvidenceHTTP", return_value=HTTP()), redirect_stdout(output):
                    code = main(["--inventory", str(config), "--context", "chosen-context", "sample", "--service", "checkout"])
            self.assertEqual(code, 0)
            reader.assert_called_once_with("chosen-context")
            self.assertEqual(json.loads(output.getvalue())["evidence"]["sample"]["target"]["uid"], "checkout-uid")

    def test_bad_inventory_cannot_produce_health_output(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "inventory.json"
            config.write_text("{}", encoding="utf-8")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["--inventory", str(config), "--context", "chosen-context", "sample", "--service", "checkout"])
            self.assertEqual(code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(json.loads(errors.getvalue())["kind"], "verifier_evidence_error")
