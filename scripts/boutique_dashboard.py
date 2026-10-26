"""Manage the read-only dashboard and telemetry for one Boutique experiment.

The collector never writes Coordinator records or participates in authorization.
The experiment owns all processes/threads so cleanup needs no PID guessing.
"""

import json
from pathlib import Path
import threading
from urllib.request import urlopen

from runtime.cluster_metrics import collect_cluster_snapshot
from runtime.dashboard import make_server
from runtime.telemetry import emit_cluster_snapshot, emit_event
from scripts.dashboard import ensure_frontend
from scripts.test_executor_live import require, save


class BoutiqueDashboard:
    def __init__(self, directory, *, context, cluster_id, namespace, port=8765, bind="127.0.0.1"):
        self.directory = Path(directory)
        self.telemetry = self.directory / "telemetry.sqlite3"
        self.context, self.cluster_id, self.namespace = context, cluster_id, namespace
        self.port = port
        self.bind = bind
        self.stop = threading.Event()
        self.server = None
        self.threads = []

    def start(self):
        ensure_frontend()
        self.server = make_server(self.directory / "coordinator.sqlite", self.telemetry, port=self.port, bind=self.bind)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.event(1, "status", "Live Boutique experiment; harness planning; Role 6 dependency and outcome verification integrated")
        self.threads = [threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .25}, daemon=True),
                        threading.Thread(target=self.collect, daemon=True)]
        for thread in self.threads:
            thread.start()
        save(self.directory / "dashboard-session.json", {
            "url": self.url, "mode": "recorded/live producers", "context": self.context,
            "coordinator": str(self.directory / "coordinator.sqlite"), "telemetry": str(self.telemetry)})
        print("Live Boutique dashboard: " + self.url, flush=True)

    def event(self, component, event_type, summary, **details):
        return emit_event(self.telemetry, component=component, event_type=event_type, summary=summary, **details)

    def collect(self):
        while not self.stop.is_set():
            try:
                cluster = collect_cluster_snapshot(context=self.context, cluster_id=self.cluster_id, namespace=self.namespace)
                emit_cluster_snapshot(self.telemetry, cluster)
                self.event(2, "heartbeat", "Dashboard cluster inventory collected; missing usage metrics remain unavailable")
            except Exception as error:
                # Logs and credentials from subprocess exceptions are not published.
                self.event(2, "error", "Dashboard collection failed: " + type(error).__name__)
            self.stop.wait(2)

    def verify(self):
        """Check the real HTTP surface against this run's persisted producers."""
        with urlopen(self.url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            require(response.status == 200 and "/assets/" in html, "Built dashboard frontend was not served")
        with urlopen(self.url + "/api/snapshot", timeout=5) as response:
            snapshot = json.load(response)
        require(snapshot["mode"] == "recorded/live producers", "Dashboard must not use synthetic playback")
        require(snapshot["sources"]["coordinator"]["connected"], "Dashboard cannot read experiment Coordinator")
        require(snapshot["sources"]["telemetry"]["connected"], "Dashboard cannot read experiment telemetry")
        require(len(snapshot["incidents"]) == 2 and all(i["state"] == "RECOVERED" for i in snapshot["incidents"]),
                "Dashboard must show two incidents closed by Role 6")
        require(len(snapshot["actions"]) == 2 and all(a["execution_result"]["status"] == "APPLIED" for a in snapshot["actions"]),
                "Dashboard must show the two real execution results")
        require(bool(snapshot["reports"]) and len(snapshot["approvals"]) == 2, "Guard reports or approvals missing from UI data")
        outcomes = [item["message"] for item in snapshot["messages"] if item["message"]["kind"] == "outcome"]
        dependencies = [item["message"] for item in snapshot["messages"] if item["message"]["kind"] == "dependency_result"]
        require(len(outcomes) == 2 and all(item["status"] == "RECOVERED"
                                          and all(check["passed"] for check in item["checks"]) for item in outcomes),
                "Dashboard must expose two passing Role 6 outcomes")
        require(len(dependencies) == 2 and all(item["status"] == "CLEAR" for item in dependencies),
                "Dashboard must expose two Role 6 pre-execution dependency results")
        history = snapshot["cluster_history"]
        require(history and all(c["cluster_id"] == self.cluster_id and c["namespace"] == self.namespace for c in history),
                "Dashboard cluster history is missing or belongs to another experiment")
        require(any(p["name"].startswith("shippingservice-") for c in history for p in c["pods"]),
                "Dashboard has not observed the actual shipping Pods")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "url": self.url, "frontend_http": 200, "snapshot_http": 200,
                  "mode": snapshot["mode"], "incidents": len(snapshot["incidents"]),
                  "guard_reports": len(snapshot["reports"]), "applied_actions": len(snapshot["actions"]),
                  "cluster_samples": len(history), "role6_outcomes": len(outcomes), "dependency_results": len(dependencies)}
        save(self.directory / "dashboard-validation.json", result)
        return result

    def close(self):
        self.stop.set()
        if self.server:
            if self.threads and self.threads[0].is_alive():
                self.server.shutdown()
            self.server.server_close()
        for thread in self.threads:
            thread.join(timeout=25)
        require(not any(t.is_alive() for t in self.threads), "Dashboard collector did not stop within its bounded request window")
