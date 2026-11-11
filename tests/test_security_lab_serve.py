"""The protected-lab dashboard keeps serving while the security gateway has no Pod."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.security import boutique_security as lab


class InspectionCollectionTests(unittest.TestCase):
    def test_gateway_without_pods_is_skipped_until_the_next_poll(self):
        # kubectl logs waits for a Pod while the gateway is scaled to zero; its timeout
        # used to end the serve loop and take the dashboard down.
        timeout = subprocess.TimeoutExpired(["kubectl", "logs"], 8)
        with tempfile.TemporaryDirectory() as temp, patch.object(lab, "kube", side_effect=timeout), \
                patch.object(lab, "import_inspections") as store:
            self.assertIsNone(lab.collect_inspections(Path(temp)))
        store.assert_not_called()

    def test_inspection_events_are_still_imported(self):
        output = '{"kind": "inspection", "decision": "ALLOW"}\nnot json\n{"kind": "other"}\n'
        completed = subprocess.CompletedProcess(["kubectl"], 0, output, "")
        with tempfile.TemporaryDirectory() as temp, patch.object(lab, "kube", return_value=completed), \
                patch.object(lab, "import_inspections") as store:
            lab.collect_inspections(Path(temp))
        store.assert_called_once()
        self.assertEqual(store.call_args.args[1], [{"kind": "inspection", "decision": "ALLOW"}])


if __name__ == "__main__":
    unittest.main()
