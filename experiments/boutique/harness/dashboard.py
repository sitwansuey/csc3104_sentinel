"""Manage the read-only dashboard and telemetry for one Boutique experiment.

The collector never writes Coordinator records or participates in authorization.
The experiment owns all processes/threads so cleanup needs no PID guessing.
"""

from datetime import datetime
import json
from pathlib import Path
import threading
from urllib.request import urlopen

from runtime.cluster_metrics import collect_cluster_snapshot
from runtime.boutique_observability import emit_observation
from runtime.dashboard import make_server
from runtime.telemetry import emit_cluster_snapshot, emit_event
from scripts.dashboard import ensure_frontend
from experiments.boutique.harness import targets
from experiments.boutique.harness.targets import default_target
from experiments.recovery.live import require, save
from shared import log

LOG = log.get_logger("dashboard")


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class BoutiqueDashboard:
    def __init__(self, directory, *, context, cluster_id, namespace, port=8765, bind="127.0.0.1", lab_control=None,
                 application_collector=None, approval_relay=None, target=None):
        self.directory = Path(directory)
        self.telemetry = self.directory / "telemetry.sqlite3"
        self.context, self.cluster_id, self.namespace = context, cluster_id, namespace
        # The run's recovery target; the scenario catalog's default when None.
        self.target = target or default_target()
        self.port = port
        self.bind = bind
        self.lab_control = lab_control
        self.approval_relay = approval_relay
        self.application_collector = application_collector
        self.stop = threading.Event()
        self.server = None
        self.threads = []

    def start(self):
        ensure_frontend()
        self.server = make_server(self.directory / "coordinator.sqlite", self.telemetry, port=self.port,
                                  bind=self.bind, lab_control=self.lab_control, approval_relay=self.approval_relay)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.event(1, "status", "Live Boutique experiment; configured proposal source; Role 6 dependency and outcome verification integrated")
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
            except Exception as error:  # noqa: BLE001 - the dashboard shows the failure and retries
                # Logs and credentials from subprocess exceptions are not published.
                log.failure(LOG, "dashboard.collection.failed", error)
                self.event(2, "error", "Dashboard collection failed: " + type(error).__name__)
            if self.application_collector:
                try:
                    emit_observation(self.telemetry, cluster_id=self.cluster_id, namespace=self.namespace,
                                     stage="inventory", services=self.application_collector())
                except Exception as error:  # noqa: BLE001 - the dashboard shows the failure and retries
                    log.failure(LOG, "dashboard.inventory.failed", error)
                    self.event(2, "error", "Boutique inventory collection failed: " + type(error).__name__)
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

    def _authenticated_approvals(self, snapshot, folders, operator_id):
        """Each recovery's approval came through approval_http from the configured identity.

        The dashboard hides a managed approval's record, so the operator identity
        is read from the approval the harness saved in each recovery folder.
        """
        require(isinstance(operator_id, str) and operator_id, "The configured approval identity is required")
        for folder in folders:
            saved = json.loads((self.directory / folder / "approval.json").read_text(encoding="utf-8"))
            require(isinstance(saved, dict) and saved.get("operator_id") == operator_id,
                    "Approval in " + folder + " is not from the configured identity")
            shown = [item["record"] for item in snapshot["approvals"]
                     if item["record"].get("action_id") == saved.get("action_id")]
            require(len(shown) == 1 and shown[0].get("protected") is True,
                    "Approval in " + folder + " was not created through the authenticated approval API")

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
        from experiments.boutique.harness.rejection import validate_rejection_proof
        validate_rejection_proof(proof, incident_id=incident["incident_id"],
                                 action_id=action["action_id"], target_uid=incident["target"]["uid"])
        require(any(event["component"] == 5 and event["action_id"] == action["action_id"]
                    and event["incident_id"] == incident["incident_id"]
                    and event["target"] == incident["target"]
                    and "0 patch attempts" in event["summary"] for event in snapshot["telemetry"]),
                "Recorded Executor refusal evidence is missing from the UI")

    def verify_agent(self, expect="recovered", *, operator_id=None):
        """Validate the single agent-driven case using the real dashboard API.

        expect="recovered" checks the guarded recovery, including that its one
        approval came through approval_http from operator_id. expect="escalated"
        checks the other accepted ending: planning ended in a saved ESCALATE, the
        incident is NEEDS_REVIEW, and nothing reached Guard, approval, the Executor
        or the Verifier; that run asks for no approval, so it has no operator_id.
        """
        snapshot = self._snapshot()
        require(len(snapshot["incidents"]) == len(snapshot["actions"]) == 1,
                "Agent dashboard must show exactly one incident and action")
        incident, action = snapshot["incidents"][0], snapshot["actions"][0]
        if expect == "escalated":
            return self._verify_agent_escalation(snapshot, incident, action)
        require(expect == "recovered", "Unknown agent dashboard expectation")
        self._action_scope(incident, action)
        require(incident["state"] == action["state"] == "RECOVERED"
                and action["guard_result"]["verdict"] == "ALLOW"
                and action["execution_result"]["status"] == "APPLIED",
                "Agent dashboard must show a guarded, applied and verified recovery")
        outcomes = [m["message"] for m in snapshot["messages"] if m["message"]["kind"] == "outcome"]
        require(len(outcomes) == 1 and outcomes[0]["status"] == "RECOVERED"
                and outcomes[0]["checks"] and all(c["passed"] for c in outcomes[0]["checks"])
                and all(outcomes[0][k] == action["proposal"][k] for k in ("incident_id", "action_id", "target")),
                "Agent dashboard must expose the matching passing Verifier outcome")
        require(len(snapshot["approvals"]) == 1
                and snapshot["approvals"][0]["record"]["action_id"] == action["action_id"],
                "Agent dashboard must expose the matching operator approval")
        self._authenticated_approvals(snapshot, ["approved-rollback"], operator_id)
        history = snapshot["cluster_history"]
        require(history and all(h["cluster_id"] == self.cluster_id and h["namespace"] == self.namespace for h in history),
                "Agent dashboard cluster evidence is missing or belongs to another scope")
        calls = json.loads((self.directory / "approved-rollback/model-calls.json").read_text(encoding="utf-8"))
        require(any(c["role"] == "Recovery Planner" for c in calls), "No actual model planning call was recorded")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "scenario": "live-agent", "url": self.url,
                  "incidents": 1, "applied_actions": 1, "role6_outcomes": 1, "model_calls": len(calls)}
        save(self.directory / "dashboard-check.json", result)
        return result

    def _verify_agent_escalation(self, snapshot, incident, action):
        proposal = action["proposal"]
        require(action["incident_id"] == incident["incident_id"] == proposal["incident_id"]
                and incident["active_action_id"] == action["action_id"] == proposal["action_id"]
                and proposal["target"] == incident["target"],
                "Dashboard action does not match its incident")
        require(incident["state"] == action["state"] == "NEEDS_REVIEW" and proposal["action"] == "ESCALATE",
                "Agent dashboard must show the saved ESCALATE and the incident in NEEDS_REVIEW")
        require(action["guard_result"] is None and action["execution_result"] is None
                and not snapshot["approvals"]
                and not any(row["topic"] in {"guard.request", "executor.request"} for row in snapshot["outbox"])
                and not any(m["message"]["kind"] in {"execution_result", "outcome"} for m in snapshot["messages"]),
                "An escalated agent case must not reach Guard, approval, the Executor or the Verifier")
        # The approval authority writes this file when it starts; an escalated run never asks for approval.
        require(not (self.directory / "approval-identities.json").exists(),
                "An escalated agent case must not start the approval session")
        status = json.loads((self.directory / "approved-rollback/planning-status.json").read_text(encoding="utf-8"))
        require(status["proposal"] == proposal and status["delivered"],
                "The Planner journal must hold the delivered ESCALATE")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "scenario": "live-agent-escalated", "url": self.url, "incidents": 1,
                  "applied_actions": 0, "error_code": status["error_code"], "attempts": status["attempts"]}
        save(self.directory / "dashboard-check.json", result)
        return result

    def verify(self, *, operator_id):
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
        self._authenticated_approvals(snapshot, ["approved-rollback", "crash-after-write"], operator_id)
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
        require(any(p["name"].startswith(self.target + "-") for c in history for p in c["pods"]),
                "Dashboard has not observed the actual " + self.target + " Pods")
        save(self.directory / "dashboard-snapshot.json", snapshot)
        result = {"status": "PASS", "url": self.url, "frontend_http": 200, "snapshot_http": 200,
                  "mode": snapshot["mode"], "incidents": len(snapshot["incidents"]),
                  "guard_reports": len(snapshot["reports"]), "applied_actions": len(recovered),
                  "rejected_actions": len(rejected), "executor_patch_attempts_for_rejected_action": 0,
                  "cluster_samples": len(history), "role6_outcomes": len(outcomes), "dependency_results": len(dependencies)}
        if self.application_collector:
            observations = snapshot.get("boutique_history", [])
            require(observations and all(item["cluster_id"] == self.cluster_id and item["namespace"] == self.namespace
                                         for item in observations), "Boutique observations belong to another experiment")
            inventory = next((item for item in observations if item["stage"] == "inventory"), None)
            require(inventory and len(inventory["services"]) == 11,
                    "Dashboard must expose all eleven Boutique Deployments")
            # The predicted storefront impact is per target in the catalog: a broken shipping
            # configuration leaves home and add to cart working, a broken cart breaks every step.
            profile = targets.profile(self.target)
            for stage in ("primary-fault", "crash-fault"):
                measured = next((item for item in observations if item["stage"] == stage), None)
                require(measured and measured["journey"]["passed"] is False
                        and targets.impact_matches(profile, measured["journey"]),
                        "Dashboard must show actual downstream shopping failures")
            measured = [item for item in observations if item["stage"] == "verification"]
            # A failed journey is acceptable only as convergence evidence the outcome itself discloses.
            windows = {item["action_id"]: _instant(item["window_start"]) for item in outcomes
                       if any(check.get("name") == "convergence" and check.get("detail", "").startswith("Excluded ")
                              for check in item["checks"])}
            confirmed = [item for item in measured
                         if item["journey"]["passed"] is True and item["journey"]["order_confirmed"]]
            require(len(confirmed) >= 6 and all(
                item in confirmed or (item.get("action_id") in windows
                                      and _instant(item["observed_at"]) < windows[item["action_id"]])
                for item in measured), "Dashboard must show confirmed post-action shopping results")
            result["boutique"] = {"deployments": len(inventory["services"]), "fault_measurements": 2,
                                  "post_action_measurements": len(confirmed),
                                  "convergence_measurements": len(measured) - len(confirmed)}
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
        control_error = None
        if self.approval_relay:
            self.approval_relay.close()  # A waiting dashboard approver stops waiting; nothing more is relayed.
        if self.lab_control:
            try:
                self.lab_control.finish("STOPPED")
            except OSError as error:
                control_error = error
        self.stop.set()
        if self.server:
            if self.threads and self.threads[0].is_alive():
                self.server.shutdown()
            self.server.server_close()
        for thread in self.threads:
            thread.join(timeout=35)
        require(not any(t.is_alive() for t in self.threads), "Dashboard collector did not stop within its bounded request window")
        if control_error:
            raise control_error
