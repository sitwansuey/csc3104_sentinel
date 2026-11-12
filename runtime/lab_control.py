"""One-session gates for the existing, fixed Boutique experiment.

HTTP threads can release a waiting scenario, never execute Kubernetes commands.
The harness stays the sole owner of injection, role workers and their databases.
Saved state is an audit record; it does not resume mutations after a restart.
The lab steps and the allowed (cluster, context, namespace, deployment) scopes
come from the scenario catalog, experiments/boutique/catalog.json.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hmac
import json
from pathlib import Path
import secrets
import threading
from uuid import uuid4

from experiments.boutique.harness.scenarios import lab_scopes, lab_sequence, load


_CATALOG = load()
SCENARIOS = lab_sequence(_CATALOG)
SCOPES = lab_scopes(_CATALOG)


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class LabConflict(ValueError):
    """A click does not match the current ready gate; no scenario was released."""


class LabControl:
    def __init__(self, directory, *, cluster_id, context, namespace, deployment, rejection_only=False):
        if (cluster_id, context, namespace, deployment) not in SCOPES:
            raise ValueError("UI injection is limited to the dedicated Boutique lab")
        self._condition = threading.Condition()
        self._token = secrets.token_urlsafe(32)
        self._accepted = set()
        self._closed = False
        self._path = Path(directory) / "lab-session.json"
        selected = SCENARIOS[2:] if rejection_only else SCENARIOS
        self._state = {
            "session_id": uuid4().hex, "state": "PREPARING", "gate_id": None,
            "active_scenario": None, "can_inject": False, "updated_at": timestamp(),
            "target": {"cluster_id": cluster_id, "namespace": namespace, "name": deployment},
            "planning": "Harness-authored proposals; no model call",
            "scenarios": [dict(id=key, title=title, description=description, expected=expected,
                               status="PENDING", requested_at=None, finished_at=None, incident_id=None,
                               action_id=None) for key, title, description, expected in selected],
        }
        self._persist()

    def _persist(self):
        self._state["updated_at"] = timestamp()
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._state, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(self._path)

    def snapshot(self):
        with self._condition:
            return deepcopy(self._state)

    def capabilities(self):
        # This secret is returned only by the local API, never saved in run data.
        with self._condition:
            return {"session_id": self._state["session_id"], "control_token": self._token}

    def authenticates(self, token):
        return (isinstance(token, str) and token.isascii()
                and hmac.compare_digest(token, self._token))

    def wait_for_injection(self, scenario_id):
        with self._condition:
            if self._closed:
                raise InterruptedError("Lab controls stopped")
            pending = next((item for item in self._state["scenarios"] if item["status"] == "PENDING"), None)
            if (pending is None or pending["id"] != scenario_id
                    or any(item["status"] in {"READY", "RUNNING"} for item in self._state["scenarios"])):
                raise LabConflict("Scenario does not follow the configured experiment sequence")
            gate_id = uuid4().hex
            pending["status"] = "READY"
            self._state.update(state="READY", gate_id=gate_id, active_scenario=scenario_id, can_inject=True)
            self._persist()
            self._condition.notify_all()
            while gate_id not in self._accepted and not self._closed:
                self._condition.wait(timeout=.5)
            if self._closed:
                raise InterruptedError("Lab controls stopped")

    def request_injection(self, session_id, gate_id):
        with self._condition:
            if self._closed or session_id != self._state["session_id"] or not isinstance(gate_id, str):
                raise LabConflict("This experiment session is no longer available")
            if gate_id in self._accepted:
                return {"accepted": True, "duplicate": True}
            if not self._state["can_inject"] or gate_id != self._state["gate_id"]:
                raise LabConflict("The scenario changed; refresh before injecting")
            selected = next(item for item in self._state["scenarios"]
                            if item["id"] == self._state["active_scenario"])
            previous = deepcopy(self._state)
            selected.update(status="RUNNING", requested_at=timestamp())
            self._state.update(state="RUNNING", can_inject=False)
            try:
                self._persist()  # Do not release the harness if saving the audit fails.
            except OSError:
                self._state = previous
                raise
            self._accepted.add(gate_id)
            self._condition.notify_all()
            return {"accepted": True, "duplicate": False}

    def complete_scenario(self, outcome, *, incident_id, action_id):
        with self._condition:
            selected = next((item for item in self._state["scenarios"]
                             if item["id"] == self._state["active_scenario"]), None)
            if selected is None or selected["status"] != "RUNNING" or outcome != selected["expected"]:
                raise LabConflict("Scenario did not produce its expected measured outcome")
            selected.update(status=outcome, finished_at=timestamp(), incident_id=incident_id, action_id=action_id)
            self._state.update(state="BETWEEN_SCENARIOS", can_inject=False, gate_id=None)
            self._persist()

    def finish(self, state):
        if state not in {"COMPLETED", "FAILED", "STOPPED"}:
            raise ValueError("Invalid lab terminal state")
        with self._condition:
            if self._closed:
                return
            if state == "COMPLETED" and any(item["status"] != item["expected"] for item in self._state["scenarios"]):
                raise LabConflict("Cannot complete an experiment with unfinished scenarios")
            self._closed = True
            self._state.update(state=state, can_inject=False, gate_id=None)
            for item in self._state["scenarios"]:
                if item["status"] in {"READY", "RUNNING"}:
                    item.update(status=state, finished_at=timestamp())
            self._persist()
            self._condition.notify_all()
