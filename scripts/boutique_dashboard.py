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

    def _snapshot(self):
        """Read and validate the dashboard's real HTTP and database surface."""
        with urlopen(self.url + "/", timeout=5) as response:
            html = response.read().decode("utf-8")
            require(response.status == 200 and "/assets/" in html, "Built dashboard frontend was not served")
        with urlopen(self.url + "/api/snapshot", timeout=5) as response:
            snapshot = json.load(response)
        require(snapshot["mode"] == "recorded/live producers", "Dashboard must not use synthetic playback")
        require(snapshot["sources"]["coordinator"]["connected"], "Dashboard cannot read experiment Coordinator")
        require(snapshot["sources"]["telemetry"]["connected"], "Dashboard cannot read experiment telemetry")
        return snapshot

    @staticmethod
    def _action_scope(incident, action):
        require(action["incident_id"] == incident["incident_id"]
                and incident["active_action_id"] == action["action_id"],
                "Dashboard action does not match its incident")
        for message in (action["proposal"], action["guard_result"]):
            require(message is not None and message["incident_id"] == incident["incident_id"]
                    and message["action_id"] == action["action_id"] and message["target"] == incident["target"],
                    "Dashboard proposal or Guard decision belongs to another incident, action or target")

    def _rejection(self, snapshot, incident, action):
        """Bind the refused action to its exact Guard report and zero-write proof."""
        self._action_scope(incident, action)
        require(incident["state"] == action["state"] == "REJECTED", "Rejected proposal is not correctly scoped")
        decision = action["guard_result"]
        require(decision["verdict"] == "REJECT" and "ROLLBACK_ALREADY_AT_BASELINE" in decision["reason_codes"],
                "Unnecessary-rollback reason missing from the dashboard")
        require(action["execution_result"] is None and not any(
            row["topic"] == "executor.request" and row["action_id"] == action["action_id"]
            for row in snapshot["outbox"]), "Rejected proposal must not schedule or claim an execution")
        require(any(report["incident_id"] == incident["incident_id"]
                    and report["action_id"] == action["action_id"] and report["verdict"] == "REJECT"
                    and report["inputs_sha256"] == decision["inputs_sha256"]
                    and report["checked_at"] == decision["checked_at"] for report in snapshot["reports"]),
                "The exact rejected Guard findings are missing")
        require(not any(row["message"]["kind"] in {"execution_result", "outcome"}
                        and row["message"]["action_id"] == action["action_id"] for row in snapshot["messages"]),
                "Refused modification must not claim execution or a recovery outcome")
        proof = json.loads((self.directory / "unnecessary-rollback" / "proof.json").read_text(encoding="utf-8"))
        from scripts.guard_rejection_demo import validate_rejection_proof
        validate_rejection_proof(proof, incident_id=incident["incident_id"],
                                 action_id=action["action_id"], target_uid=incident["target"]["uid"])
        require(any(event["component"] == 5 and event["action_id"] == action["action_id"]
                    and event["incident_id"] == incident["incident_id"]
                    and event["target"] == incident["target"]
                    and "0 patch attempts" in event["summary"] for event in snapshot["telemetry"]),
                "Recorded Executor refusal evidence is missing from the UI")

    def verify(self):
        """Validate two real recoveries followed by a refused unnecessary rollback."""
        snapshot = self._snapshot()
        require(len(snapshot["incidents"]) == len(snapshot["actions"]) == 3,
                "Dashboard must show two recovered incidents and the final rejected recommendation")
        incidents = {item["incident_id"]: item for item in snapshot["incidents"]}
        actions = {item["action_id"]: item for item in snapshot["actions"]}
        require(len(incidents) == len(actions) == 3
                and {action["incident_id"] for action in actions.values()} == set(incidents),
                "Dashboard incident/action identities are duplicated or incomplete")
        recovered, rejected = [], []
        for action in actions.values():
            incident = incidents[action["incident_id"]]
            self._action_scope(incident, action)
            if incident["state"] == "RECOVERED":
                result = action["execution_result"]
                require(action["state"] == "RECOVERED" and action["guard_result"]["verdict"] == "ALLOW"
                        and result is not None and result["status"] == "APPLIED"
                        and all(result[key] == action["proposal"][key] for key in ("incident_id", "action_id", "target")),
                        "Dashboard must show the matching real execution for each recovery")
                recovered.append(action)
            elif incident["state"] == "REJECTED":
                self._rejection(snapshot, incident, action)
                rejected.append(action)
        require(len(recovered) == 2 and len(rejected) == 1,
                "Dashboard must show exactly two recoveries and one rejected recommendation")
        recovered_ids = {action["action_id"] for action in recovered}
        require(len(snapshot["approvals"]) == 2 and
                {item["record"]["action_id"] for item in snapshot["approvals"]} == recovered_ids,
                "Dashboard must show the two recovery approvals")
        outcomes = [item["message"] for item in snapshot["messages"] if item["message"]["kind"] == "outcome"]
        dependencies = [item["message"] for item in snapshot["messages"] if item["message"]["kind"] == "dependency_result"]
        require(len(outcomes) == 2 and all(item["status"] == "RECOVERED"
                                          and item["checks"] and all(check["passed"] for check in item["checks"])
                                          for item in outcomes)
                and {item["action_id"] for item in outcomes} == recovered_ids,
                "Dashboard must expose two passing Role 6 outcomes")
        require(len(dependencies) == 3 and all(item["status"] == "CLEAR" for item in dependencies)
                and {item["action_id"] for item in dependencies} == set(actions),
                "Dashboard must expose three Role 6 pre-execution dependency results")
        require(all(all(item[key] == actions[item["action_id"]]["proposal"][key]
                        for key in ("incident_id", "action_id", "target")) for item in outcomes + dependencies),
                "Dashboard Role 6 evidence belongs to another incident, action or target")
        history = snapshot["cluster_history"]
        require(history and all(c["cluster_id"] == self.cluster_id and c["namespace"] == self.namespace for c in history),
                "Dashboard cluster history is missing or belongs to another experiment")
        require(any(p["name"].startswith("shippingservice-") for c in history for p in c["pods"]),
                "Dashboard has not observed the actual shipping Pods")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "url": self.url, "frontend_http": 200, "snapshot_http": 200,
                  "mode": snapshot["mode"], "incidents": len(snapshot["incidents"]),
                  "guard_reports": len(snapshot["reports"]), "applied_actions": len(recovered),
                  "rejected_actions": len(rejected), "executor_patch_attempts_for_rejected_action": 0,
                  "cluster_samples": len(history), "role6_outcomes": len(outcomes), "dependency_results": len(dependencies)}
        save(self.directory / "dashboard-validation.json", result)
        return result

    def verify_rejection(self):
        """Validate the distinct false-proposal scenario without claiming recovery."""
        snapshot = self._snapshot()
        require(len(snapshot["incidents"]) == len(snapshot["actions"]) == 1,
                "Rejection demo must contain exactly one injected false alarm and proposal")
        incident, action = snapshot["incidents"][0], snapshot["actions"][0]
        self._rejection(snapshot, incident, action)
        require(not any(row["message"]["kind"] == "outcome" for row in snapshot["messages"]),
                "Refused modification must not claim a recovery outcome")
        history = snapshot["cluster_history"]
        require(history and all(item["cluster_id"] == self.cluster_id and item["namespace"] == self.namespace
                                for item in history), "Live cluster history belongs to another experiment")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "scenario": "guard-rejection", "url": self.url,
                  "frontend_http": 200, "snapshot_http": 200, "incident_id": incident["incident_id"],
                  "guard_verdict": "REJECT", "executor_patch_attempts": 0, "executor_routes": 0,
                  "workload_unchanged": True}
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
