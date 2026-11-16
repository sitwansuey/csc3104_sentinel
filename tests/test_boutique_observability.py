"""Offline projections/journal tests, with fake Kubernetes objects and HTTP data."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from runtime.boutique_observability import emit_observation, service_inventory, shopping_summary
from runtime.dashboard import snapshot
from experiments.boutique.harness.connected import APPLICATIONS, ConnectedBoutiqueExperiment
from tests.test_boutique_connected import resources


class BoutiqueObservabilityTests(unittest.TestCase):
    def test_readiness_checks_generation_replicas_and_missing_objects(self):
        deployments = [r for r in resources() if r.get("kind") == "Deployment" and r["metadata"]["name"] in APPLICATIONS]
        original = deepcopy(deployments)
        self.assertTrue(all(item["ready_state"] for item in service_inventory(deployments, APPLICATIONS)))
        self.assertEqual(deployments, original)
        deployment = deployments[0]
        name = deployment["metadata"]["name"]
        deployment["status"]["observedGeneration"] = 0
        self.assertFalse(service_inventory(deployments, [name])[0]["ready_state"])
        deployment["status"]["readyReplicas"] = True
        self.assertIsNone(service_inventory(deployments, [name])[0]["ready_state"])
        self.assertIsNone(service_inventory([], [name])[0]["ready_state"])
        deployment["spec"]["replicas"] = 0
        deployment["status"]["readyReplicas"] = 0
        self.assertFalse(service_inventory(deployments, [name])[0]["ready_state"])

    def test_projection_excludes_raw_data_and_preserves_unknown_failed_requests(self):
        result = shopping_summary({"home_status": 200, "add_to_cart_status": 302, "cart_status": 500,
            "checkout_status": None, "passed": None, "html": "private", "cookie": "secret",
            "order_confirmation_found": True, "order_page_confirmed": True})
        self.assertIsNone(result["passed"])
        self.assertEqual(result["cart_status"], 500)
        self.assertIsNone(result["checkout_status"])
        self.assertFalse(result["order_confirmed"])
        self.assertNotIn("private", json.dumps(result))
        self.assertNotIn("secret", json.dumps(result))

    def test_journal_is_optional_bounded_and_inventory_cannot_evict_journeys(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "telemetry.sqlite3"
            missing = Path(directory) / "missing.sqlite"
            self.assertNotIn("boutique_history", snapshot(missing, journal))
            self.assertFalse(journal.exists())
            emit_observation(journal, cluster_id="cluster", namespace="namespace", stage="primary-fault",
                             journey={"passed": False, "cart_status": 500}, incident_id="INC-1")
            for _ in range(65):
                emit_observation(journal, cluster_id="cluster", namespace="namespace", stage="inventory", services=[])
            recorded = snapshot(missing, journal)["boutique_history"]
            self.assertEqual(len(recorded), 61)
            self.assertEqual(recorded[-1]["incident_id"], "INC-1")
            self.assertEqual(recorded[-1]["journey"]["cart_status"], 500)
            self.assertGreater(recorded[0]["sequence"], recorded[-1]["sequence"])

    def test_live_adapter_collects_only_read_only_deployments_and_denial_stays_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = ConnectedBoutiqueExperiment(Path(directory) / "experiment")
            try:
                experiment.kubectl = Mock(return_value=SimpleNamespace(returncode=1, stdout=""))
                inventory = experiment.application_inventory()
                self.assertEqual(len(inventory), 11)
                self.assertTrue(all(item["ready_state"] is None for item in inventory))
                self.assertEqual(sum(item["recovery_target"] for item in inventory), 1)
                experiment.kubectl.assert_called_once_with("get", "deployments", "-o", "json", timeout=8, allow_failure=True)
            finally:
                experiment.store.close()

    @staticmethod
    def journaled_stages(harness, callers):
        """Every stage the given sources journal: literal record_shopping stages, each fault label with the
        suffix a record_shopping(label + "...") call adds, and literal stage= arguments of emit_observation."""
        import re
        harness_text = "\n".join(harness)
        literal = set(re.findall(r'record_shopping\(\s*"([^"]+)"', harness_text))
        suffixes = set(re.findall(r'record_shopping\(\s*label\s*\+\s*"([^"]+)"', harness_text))
        labels = {match for text in callers for match in re.findall(r'\bself\.fault\(\s*"([^"]+)"', text)}
        emitted = {match for text in (*harness, *callers)
                   for match in re.findall(r'emit_observation\([^()]*?\bstage="([^"]+)"', text, re.DOTALL)}
        return literal | {label + suffix for label in labels for suffix in suffixes} | emitted, labels, suffixes

    def test_every_stage_a_harness_journals_is_accepted(self):
        """Found live (P4.05): the pair harness journaled pair-fault, which the journal refused, so every pair
        run failed after injecting both faults. Every stage a harness journals must be known: literal
        record_shopping stages, each fault label plus the "-fault" suffix the harness adds, and the dashboard's
        and the security lab's direct emit_observation calls."""
        from runtime.boutique_observability import STAGES
        root = Path(__file__).resolve().parents[1]
        harness = [path.read_text(encoding="utf-8") for path in (root / "experiments/boutique/harness").glob("*.py")]
        callers = [(root / relative).read_text(encoding="utf-8") for relative in (
            "experiments/recovery/live.py", "experiments/boutique/harness/dashboard.py",
            "scripts/security/boutique_security.py")]
        used, labels, suffixes = self.journaled_stages(harness, callers)
        self.assertEqual((labels, suffixes), ({"primary", "crash"}, {"-fault"}))
        self.assertTrue({"baseline", "verification", "pair-fault", "pair-shop", "primary-fault", "crash-fault",
                         "inventory"} <= used)
        self.assertEqual(used - STAGES, set())
        # The scan itself finds an unknown label or a direct stage, so a new one cannot slip past it.
        rogue, _, _ = self.journaled_stages(['self.record_shopping(label + "-fault", impact)'],
                                            ['self.fault("rogue")', 'emit_observation(path, stage="odd", journey=j)'])
        self.assertEqual(rogue - STAGES, {"rogue-fault", "odd"})
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / "telemetry.sqlite3"
            for stage in ("pair-fault", "pair-shop"):
                emit_observation(journal, cluster_id="cluster", namespace="namespace", stage=stage,
                                 journey={"passed": True, "cart_status": 200})
            recorded = snapshot(Path(directory) / "missing.sqlite", journal)["boutique_history"]
            self.assertEqual({item["stage"] for item in recorded}, {"pair-fault", "pair-shop"})

    def test_invalid_stage_or_mixed_record_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            for arguments in ({"stage": "unknown"}, {"stage": "inventory", "journey": {}},
                              {"stage": "verification", "journey": {}, "services": []}):
                with self.assertRaises(ValueError):
                    emit_observation(Path(directory) / "journal", cluster_id="cluster", namespace="namespace", **arguments)
