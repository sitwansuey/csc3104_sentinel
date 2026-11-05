"""Reading opt-in lab help must not create or access a cluster."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class EntryPointHelpTests(unittest.TestCase):
    def test_observer_acceptance_help_needs_no_cluster_tools(self):
        script = Path(__file__).resolve().parents[1] / "scripts/validation/test_boutique_observer_acceptance.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(script), "--help"], cwd=directory,
                env={**os.environ, "PATH": ""}, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)
        self.assertNotIn("Observer acceptance evidence:", result.stdout)
