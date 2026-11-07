"""The final refusal must follow both recoveries and precede credential cleanup."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.demos.guard_rejection_demo import run_unnecessary_rollback
from scripts.boutique.test_boutique_live import BoutiqueExperiment


class BoutiqueSequenceTests(unittest.TestCase):
    def run_with_recoveries(self, experiment, *, crash_error=None):
        completed = []
        experiment.credentials.write_text("test-only credential marker", encoding="utf-8")

        def positive(incident):
            completed.append("verified normal recovery")

        def crash():
            if crash_error:
                raise crash_error
            completed.append("verified crash recovery")

        def refuse(current):
            self.assertIs(current, experiment)
            self.assertEqual(completed, ["verified normal recovery", "verified crash recovery"])
            self.assertTrue(current.credentials.is_file(), "Final refusal needs the restricted identity")
            self.assertEqual(current.store.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
            completed.append("final refusal")

        with patch.object(experiment, "prepare"), patch.object(experiment, "permissions"), \
                patch.object(experiment, "fault", return_value={"incident_id": "test"}), \
                patch.object(experiment, "negative_cases"), patch.object(experiment, "conditional_race"), \
                patch.object(experiment, "positive", side_effect=positive), \
                patch.object(experiment, "crash_restart", side_effect=crash), \
                patch.object(experiment, "deployment", return_value={"test": "final snapshot"}), \
                patch("scripts.demos.guard_rejection_demo.run_unnecessary_rollback", side_effect=refuse) as final:
            if crash_error:
                with self.assertRaisesRegex(RuntimeError, "verification failed"):
                    experiment.run()
                final.assert_not_called()
            else:
                experiment.run()
                final.assert_called_once_with(experiment)
        return completed

    def test_final_refusal_follows_both_verified_recoveries_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            completed = self.run_with_recoveries(experiment)
            self.assertEqual(completed[-1], "final refusal")
            self.assertFalse(experiment.credentials.exists())
            summary = json.loads((experiment.directory / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "PASS")
            self.assertTrue(summary["private_executor_credentials_removed"])

    def test_failed_second_recovery_does_not_run_or_claim_final_refusal(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            self.run_with_recoveries(experiment, crash_error=RuntimeError("verification failed"))
            self.assertFalse(experiment.credentials.exists())
            summary = json.loads((experiment.directory / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "FAILED")
            self.assertNotIn("guard_rejection", summary)

    def test_final_enter_prompt_precedes_false_proposal_or_cluster_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = BoutiqueExperiment(Path(directory) / "run")
            experiment.pause_before_injection = True
            try:
                with patch("builtins.input", side_effect=KeyboardInterrupt) as prompt, \
                        patch("scripts.demos.guard_rejection_demo.workload_proof") as proof:
                    with self.assertRaises(KeyboardInterrupt):
                        run_unnecessary_rollback(experiment)
                    self.assertIn("unnecessary rollback proposal", prompt.call_args.args[0])
                    proof.assert_not_called()
                    self.assertEqual(experiment.store.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 0)
            finally:
                experiment.store.close()


if __name__ == "__main__":
    unittest.main()
