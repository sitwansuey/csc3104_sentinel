"""Offline dashboard lifecycle checks; cluster data is explicitly fake here."""

import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import urlopen

from scripts.boutique_dashboard import BoutiqueDashboard
from services.coordinator import SQLiteStore


class BoutiqueDashboardTests(unittest.TestCase):
    def test_live_mode_reads_exact_run_without_creating_duplicate_incidents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "assets"
            assets.mkdir()
            (assets / "index.html").write_text('<html><script src="/assets/test.js"></script></html>')
            with SQLiteStore(root / "coordinator.sqlite") as store:
                self.assertEqual(store.db.execute("SELECT count(*) FROM incidents").fetchone()[0], 0)
            cluster = {"schema_version": "1.0", "cluster_id": "test-cluster", "namespace": "test-namespace",
                       "observed_at": "2026-10-25T00:00:00Z", "sources": {}, "nodes": [], "pods": [],
                       "events": [], "summary": {}, "truncation": {}}
            dashboard = BoutiqueDashboard(root, context="test-context", cluster_id="test-cluster",
                                          namespace="test-namespace", port=0)
            with patch("scripts.boutique_dashboard.ensure_frontend"), \
                    patch("scripts.boutique_dashboard.collect_cluster_snapshot", return_value=cluster) as collect, \
                    patch("runtime.dashboard.ASSETS", assets):
                try:
                    dashboard.start()
                    deadline = time.monotonic() + 5
                    while True:
                        with urlopen(dashboard.url + "/api/snapshot", timeout=2) as response:
                            snapshot = json.load(response)
                        if snapshot["cluster_history"] or time.monotonic() > deadline:
                            break
                        time.sleep(.02)
                    self.assertEqual(snapshot["mode"], "recorded/live producers")
                    self.assertTrue(snapshot["sources"]["coordinator"]["connected"])
                    self.assertTrue(snapshot["sources"]["telemetry"]["connected"])
                    self.assertEqual(snapshot["cluster_history"][0]["cluster_id"], "test-cluster")
                    self.assertEqual(snapshot["incidents"], [])
                    collect.assert_called_with(context="test-context", cluster_id="test-cluster", namespace="test-namespace")
                finally:
                    dashboard.close()
                self.assertFalse(any(thread.is_alive() for thread in dashboard.threads))
                with self.assertRaises(URLError):
                    urlopen(dashboard.url, timeout=1)

    def test_unstarted_session_can_be_closed_after_frontend_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            dashboard = BoutiqueDashboard(directory, context="test", cluster_id="test", namespace="test", port=0)
            with patch("scripts.boutique_dashboard.ensure_frontend", side_effect=RuntimeError("build failed")):
                with self.assertRaisesRegex(RuntimeError, "build failed"):
                    dashboard.start()
            dashboard.close()
            self.assertEqual(dashboard.threads, [])


if __name__ == "__main__":
    unittest.main()
