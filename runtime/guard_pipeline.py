"""Consume durable Guard requests and record decisions in the coordinator.

Only this runtime wiring imports both services. Input readers are injected and
must provide trusted policy, baseline and state. No Kubernetes writes occur here.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

from services.coordinator import acknowledge, dispatch_pending, record_message
from services.guard import evaluate_with_report


def json_reader(path):
    """Reload a file per request so updates are not silently cached."""
    def read(proposal):
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    return read


class KubectlStateReader:
    """Read-only Deployment lookup using an explicit configured cluster context."""

    def __init__(self, context, cluster_id, *, runner=subprocess.run, clock=None):
        if not context or not cluster_id:
            raise ValueError("A Kubernetes context and Sentinel cluster ID are required")
        self.context, self.cluster_id = context, cluster_id
        self.runner = runner
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def __call__(self, proposal):
        target = proposal["target"]
        if target["cluster_id"] != self.cluster_id:
            raise ValueError("Proposal cluster ID does not match the configured Kubernetes reader")
        command = ["kubectl", "--context", self.context, "--namespace", target["namespace"],
                   "get", "deployment", target["name"], "-o", "json"]
        result = self.runner(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "Could not read current Deployment")
        return {"cluster_id": self.cluster_id, "captured_at": self.clock().isoformat().replace("+00:00", "Z"),
                "deployment": json.loads(result.stdout)}


def _dependencies(store, proposal, now):
    records = []
    for row in store.db.execute("SELECT payload FROM messages"):
        message = json.loads(row[0])
        if message["kind"] == "dependency_result" and message["action_id"] == proposal["action_id"]:
            records.append(message)
    if records:
        return max(records, key=lambda item: datetime.fromisoformat(item["checked_at"].replace("Z", "+00:00")))
    # Absence of owner 6's result must never become fabricated CLEAR evidence.
    return {"schema_version": "1.0", "kind": "dependency_result",
            "incident_id": proposal["incident_id"], "action_id": proposal["action_id"],
            "target": deepcopy(proposal["target"]), "status": "UNKNOWN",
            "affected_services": [], "blockers": [], "unknowns": ["Dependency verification is unavailable"],
            "checked_at": now.isoformat().replace("+00:00", "Z")}


def process_guard_requests(store, event_bus, *, state_reader, policy_reader,
                           baseline_reader=None, dependency_reader=None, clock=None):
    """Process currently pending Guard requests, independently of other topics.

    Reads current stored proposals/approvals, evaluates the real Guard, and
    atomically commits the decision, downstream command and request acknowledgment.
    Input failures leave requests pending and cannot create execution commands.
    Uses one SQLite transaction per request; readers should be short and bounded.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    pending = store.db.execute(
        "SELECT id FROM outbox WHERE topic='guard.request' AND delivered=0 ORDER BY id"
    ).fetchall()
    decisions = []
    for pending_row in pending:
        store.db.execute("BEGIN IMMEDIATE")
        try:
            request = store.db.execute("SELECT * FROM outbox WHERE id=?", (pending_row[0],)).fetchone()
            if request["delivered"]:
                store.db.execute("COMMIT")
                continue
            action = store.action(request["action_id"])
            incident = store.incident(action["incident_id"]) if action else None
            if (not action or incident["active_action_id"] != action["action_id"]
                    or action["state"] not in {"VALIDATING", "AWAITING_APPROVAL"}):
                acknowledge(store, request["id"])
                store.db.execute("COMMIT")
                continue
            proposal = action["proposal"]
            policy = policy_reader(deepcopy(proposal))
            baseline = baseline_reader(deepcopy(proposal)) if baseline_reader else None
            dependencies = dependency_reader(deepcopy(proposal)) if dependency_reader else None
            state = state_reader(deepcopy(proposal))
            now = clock()
            if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
                raise ValueError("Guard worker clock must return timezone-aware UTC")
            if dependencies is None:
                dependencies = _dependencies(store, proposal, now)
            decision, report = evaluate_with_report(
                proposal, state, policy, baseline, dependencies,
                store.approval(action["action_id"]), now=now)
            record_message(decision, store)
            store.db.execute(
                "INSERT OR IGNORE INTO guard_reports(decision_sha256,action_id,report) VALUES(?,?,?)",
                (report["decision_sha256"], action["action_id"], json.dumps(report, allow_nan=False)))
            acknowledge(store, request["id"])
            store.db.execute("COMMIT")
            decisions.append(decision)
        except BaseException:
            store.db.execute("ROLLBACK")
            raise
    dispatch_pending(store, event_bus)
    return decisions
