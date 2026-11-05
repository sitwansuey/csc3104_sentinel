from copy import deepcopy
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import json
import io
from pathlib import Path
import sqlite3
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from runtime.dashboard import LIMIT, make_server, snapshot
from runtime.dashboard_demo import start_demo
from runtime.guard_pipeline import process_guard_requests
from runtime.telemetry import emit_event
from scripts.observe_dashboard import collect_once
from scripts.dashboard import ensure_frontend, main as dashboard_main
from services.coordinator.core import LocalEventBus, SQLiteStore, handle
from services.observer.core import Observer

ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = Path(self.directory.name) / "coordinator.sqlite3"
        self.telemetry = Path(self.directory.name) / "telemetry.sqlite3"

    def test_missing_sources_do_not_create_database_or_claim_health(self):
        result = snapshot(self.db, self.telemetry)
        self.assertFalse(result["sources"]["coordinator"]["connected"])
        self.assertFalse(self.db.exists())
        self.assertFalse(self.telemetry.exists())
        self.assertEqual(result["incidents"], [])

    def test_startup_can_select_available_port_and_open_browser(self):
        server = Mock(server_port=12345)
        server.serve_forever.side_effect = KeyboardInterrupt
        output = io.StringIO()
        with patch("scripts.dashboard.make_server", return_value=server) as factory, \
                patch("scripts.dashboard.ensure_frontend"), \
                patch("scripts.dashboard.threading.Thread") as thread, redirect_stdout(output):
            dashboard_main(["--port", "0", "--open-browser"])
        self.assertEqual(factory.call_args.kwargs["port"], 0)
        thread.return_value.start.assert_called_once()
        server.server_close.assert_called_once()
        self.assertIn("http://127.0.0.1:12345", output.getvalue())
        self.assertIn("Keep this terminal open", output.getvalue())

    def test_port_conflict_gives_actionable_message_and_cleans_demo(self):
        output = io.StringIO()
        cleanup = Mock()
        with patch("runtime.dashboard_demo.start_demo", return_value=(lambda: {}, cleanup)), \
                patch("scripts.dashboard.ensure_frontend"), \
                patch("scripts.dashboard.make_server", side_effect=OSError("address in use")), \
                redirect_stderr(output), self.assertRaises(SystemExit) as error:
            dashboard_main(["--demo"])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("--port 0", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
        cleanup.assert_called_once()

    def test_snapshot_observes_new_messages_and_never_mutates_coordinator(self):
        with SQLiteStore(self.db) as store:
            handle(fixture("messages/incident.json"), store, LocalEventBus())
            before = self.db.read_bytes()
            first = snapshot(self.db, self.telemetry)
            self.assertEqual(before, self.db.read_bytes())
            handle(fixture("messages/proposal.json"), store, LocalEventBus())
            second = snapshot(self.db, self.telemetry)
        self.assertEqual(first["incidents"][0]["state"], "OPEN")
        self.assertEqual(second["incidents"][0]["state"], "VALIDATING")
        self.assertEqual(second["messages"][0]["message"]["kind"], "proposal")

    def test_old_database_without_reports_still_works(self):
        with SQLiteStore(self.db) as store:
            store.db.execute("DROP TABLE guard_reports")
        self.assertTrue(snapshot(self.db, self.telemetry)["sources"]["coordinator"]["connected"])

    def test_event_validation_and_bounded_history(self):
        for index in range(LIMIT + 3):
            emit_event(self.telemetry, component=1, event_type="fault", summary=f"Marker {index}")
        result = snapshot(self.db, self.telemetry)
        self.assertEqual(len(result["telemetry"]), LIMIT)
        self.assertEqual(result["telemetry"][0]["summary"], f"Marker {LIMIT + 2}")
        with self.assertRaises(ValueError):
            emit_event(self.telemetry, component=2, event_type="observation", summary="Invalid", metrics={"secret": "value"})
        with self.assertRaises(ValueError):
            emit_event(self.telemetry, component=2, event_type="error", summary="x" * 17000)

    def test_guard_worker_persists_bound_reports_and_exact_retries(self):
        with SQLiteStore(self.db) as store:
            bus = LocalEventBus()
            for name in ("incident", "proposal", "dependency-result"):
                handle(fixture(f"messages/{name}.json"), store, bus)
            # Proposal + dependency result queue two Guard requests. Both may
            # evaluate the same immutable decision; the report must not conflict.
            decisions = process_guard_requests(
                store, bus, state_reader=lambda p: fixture("observations.json")["failed-rollout"][-1],
                policy_reader=lambda p: fixture("policy.json"), baseline_reader=lambda p: fixture("baseline.json"),
                clock=lambda: datetime(2026, 10, 10, 12, 0, 35, tzinfo=timezone.utc))
            self.assertEqual(len(decisions), 2)
            report = snapshot(self.db, self.telemetry)["reports"][0]
            self.assertEqual(report["inputs_sha256"], decisions[0]["inputs_sha256"])
            self.assertTrue(any(check["status"] == "UNKNOWN" for check in report["checks"]))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM guard_reports").fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox WHERE topic='executor.request'").fetchone()[0], 0)

    def test_failed_report_write_rolls_back_decision_and_ack(self):
        with SQLiteStore(self.db) as store:
            bus = LocalEventBus()
            for name in ("incident", "proposal"):
                handle(fixture(f"messages/{name}.json"), store, bus)
            store.db.execute("CREATE TRIGGER reject_report BEFORE INSERT ON guard_reports BEGIN SELECT RAISE(ABORT, 'test'); END")
            with self.assertRaises(sqlite3.IntegrityError):
                process_guard_requests(store, bus,
                    state_reader=lambda p: fixture("observations.json")["failed-rollout"][-1],
                    policy_reader=lambda p: fixture("policy.json"), baseline_reader=lambda p: fixture("baseline.json"),
                    clock=lambda: datetime(2026, 10, 10, 12, 0, 35, tzinfo=timezone.utc))
            self.assertIsNone(store.action("ACT-DEMO-001")["guard_result"])
            self.assertEqual(store.db.execute("SELECT delivered FROM outbox WHERE topic='guard.request'").fetchone()[0], 0)

    def test_collector_only_reads_kubernetes_and_routes_real_observer_incident(self):
        observations = fixture("observations.json")["failed-rollout"]
        observer = Observer(fixture("policy.json"))
        calls = []
        with SQLiteStore(self.db) as store:
            for recorded in (observations[0], observations[-1]):
                def runner(command, **kwargs):
                    calls.append(command)
                    return subprocess.CompletedProcess(command, 0, json.dumps({"items": [recorded["deployment"]]}), "")
                collect_once(context="kind-sentinel-lab", cluster_id="sentinel-lab", namespace="sentinel-demo",
                             observer=observer, store=store, telemetry=self.telemetry, runner=runner,
                             clock=lambda: datetime.fromisoformat(recorded["captured_at"].replace("Z", "+00:00")))
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0], 1)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 0)
        self.assertEqual(calls[0], ["kubectl", "--context", "kind-sentinel-lab", "--request-timeout=8s", "get", "deployments", "--namespace", "sentinel-demo", "-o", "json"])
        telemetry = snapshot(self.db, self.telemetry)["telemetry"]
        self.assertEqual(len(telemetry), 4)
        self.assertEqual(telemetry[1]["metrics"]["ready"], 0)
        self.assertNotIn("deployment", telemetry[1])

    def test_collector_rejects_api_errors_and_malformed_lists(self):
        with SQLiteStore(self.db) as store:
            kwargs = dict(context="explicit", cluster_id="cluster", namespace="ns", observer=Observer({}),
                          store=store, telemetry=self.telemetry)
            with self.assertRaisesRegex(RuntimeError, "Kubernetes read failed"):
                collect_once(**kwargs, runner=lambda *a, **k: subprocess.CompletedProcess([], 1, "", "credential=SECRET"))
            with self.assertRaises(ValueError):
                collect_once(**kwargs, runner=lambda *a, **k: subprocess.CompletedProcess([], 0, "{}", ""))
        self.assertFalse(self.telemetry.exists())

    def test_coordinator_can_journal_failure_without_exposing_input_contents(self):
        invalid = Path(self.directory.name) / "invalid.json"
        invalid.write_text('{"credential":"DO-NOT-PUBLISH",', encoding="utf-8")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/coordinator.py"),
                                 "--db", str(self.db), "--telemetry", str(self.telemetry),
                                 "ingest", str(invalid)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        record = snapshot(self.db, self.telemetry)["telemetry"][0]
        self.assertEqual(record["component"], 1)
        self.assertEqual(record["event_type"], "error")
        self.assertNotIn("DO-NOT-PUBLISH", json.dumps(record))
        # An unwritable diagnostic destination must preserve the command failure.
        result = subprocess.run([sys.executable, str(ROOT / "scripts/coordinator.py"),
                                 "--db", str(self.db), "--telemetry", self.directory.name,
                                 "ingest", str(invalid)], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn("coordinator:", result.stderr)

    def test_demo_isolated_and_all_six_stages_available(self):
        with patch("runtime.dashboard_demo.time.monotonic", return_value=0):
            provider, cleanup = start_demo()
        self.addCleanup(cleanup)
        with patch("runtime.dashboard_demo.time.monotonic", return_value=30):
            result = provider()
        self.assertEqual(result["mode"], "synthetic")
        self.assertEqual(result["incidents"][0]["state"], "RECOVERED")
        self.assertEqual({r["message"]["kind"] for r in result["messages"]},
                         {"incident", "proposal", "guard_result", "dependency_result", "execution_result", "outcome"})
        self.assertEqual(len(result["reports"]), 2)
        self.assertFalse(self.db.exists())

    def test_http_serves_assets_and_refuses_mutations_and_traversal(self):
        assets = Path(self.directory.name) / "dist"
        (assets / "assets").mkdir(parents=True)
        (assets / "index.html").write_text('<div id="root"></div><script src="/assets/app-abc.js"></script>')
        (assets / "assets/app-abc.js").write_text("console.log('built frontend')")
        (assets / "assets/style-abc.css").write_text("body{color:white}")
        patcher = patch("runtime.dashboard.ASSETS", assets)
        patcher.start()
        self.addCleanup(patcher.stop)
        server = make_server(self.db, self.telemetry, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            for path in ("/", "/assets/app-abc.js", "/assets/style-abc.css", "/api/snapshot"):
                with urlopen(base + path, timeout=3) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.headers["Cache-Control"], "no-store")
                    self.assertNotIn("Access-Control-Allow-Origin", response.headers)
            for path in ("/../services/coordinator/core.py", "/api/approve", "/assets/../package.json", "/assets/%2e%2e/package.json", "/src/App.tsx", "/package.json", "/node_modules/react/index.js"):
                with self.assertRaises(HTTPError) as error:
                    urlopen(base + path, timeout=3)
                self.assertEqual(error.exception.code, 404)
            with self.assertRaises(HTTPError) as error:
                urlopen(Request(base + "/api/snapshot", data=b"{}", method="POST"), timeout=3)
            self.assertEqual(error.exception.code, 501)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_missing_frontend_build_returns_actionable_503(self):
        with patch("runtime.dashboard.ASSETS", Path(self.directory.name) / "missing"):
            server = make_server(self.db, self.telemetry, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(HTTPError) as error:
                    urlopen(f"http://127.0.0.1:{server.server_port}", timeout=3)
                self.assertEqual(error.exception.code, 503)
                self.assertIn("npm --prefix dashboard run build", error.exception.read().decode())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=3)

    def test_frontend_bootstrap_reuses_build_and_rebuilds_changed_sources(self):
        frontend = Path(self.directory.name) / "frontend"
        (frontend / "src").mkdir(parents=True)
        (frontend / "node_modules").mkdir()
        (frontend / "dist").mkdir()
        source = frontend / "src/App.tsx"
        source.write_text("original source")
        (frontend / "package.json").write_text("{}")
        lockfile = frontend / "package-lock.json"
        lockfile.write_text("{}")
        (frontend / "node_modules/.sentinel-package-lock.sha256").write_text(hashlib.sha256(lockfile.read_bytes()).hexdigest())
        index = frontend / "dist/index.html"
        index.write_text("built")
        newest = max(path.stat().st_mtime_ns for path in frontend.rglob("*") if path.is_file())
        os.utime(index, ns=(newest + 1000000, newest + 1000000))
        with patch("scripts.dashboard.FRONTEND", frontend), patch("scripts.dashboard.shutil.which", return_value="tool"), patch("scripts.dashboard.subprocess.run") as run:
            ensure_frontend()
            run.assert_not_called()
            source.write_text("changed source")
            def execute(command, **kwargs):
                if command[-1] == "--version":
                    return subprocess.CompletedProcess(command, 0, "v22.18.0", "")
                index.write_text("rebuilt")
                return subprocess.CompletedProcess(command, 0)
            run.side_effect = execute
            ensure_frontend()
            self.assertTrue(any(call.args[0][-2:] == ["run", "build"] for call in run.call_args_list))
            self.assertFalse(any("ci" in call.args[0] for call in run.call_args_list))
            self.assertEqual(index.read_text(), "rebuilt")

    def test_frontend_bootstrap_reports_missing_node_before_starting_server(self):
        frontend = Path(self.directory.name) / "frontend"
        frontend.mkdir()
        (frontend / "package.json").write_text("{}")
        (frontend / "package-lock.json").write_text("{}")
        with patch("scripts.dashboard.FRONTEND", frontend), patch("scripts.dashboard.shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Node.js"):
                ensure_frontend()


if __name__ == "__main__":
    unittest.main()
