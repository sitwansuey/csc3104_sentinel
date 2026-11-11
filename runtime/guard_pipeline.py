"""Consume durable Guard requests and record decisions in the coordinator.

Only this runtime wiring imports both services. Input readers are injected and
must provide trusted policy, baseline and state. No Kubernetes writes occur here.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

from services.coordinator.core import CoordinatorError, acknowledge, dispatch_pending, record_message
from services.guard.adapters import _DNS_LABEL, _DNS_SUBDOMAIN
from services.guard.core import assert_decision_usable, evaluate_with_report
from shared.contracts import validate_message


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
        namespace, name = target["namespace"], target["name"]
        # Names come from a proposal; validate them so none can reach kubectl as an option.
        if (not isinstance(namespace, str) or len(namespace) > 63 or not _DNS_LABEL.fullmatch(namespace)
                or not isinstance(name, str) or len(name) > 253 or not _DNS_SUBDOMAIN.fullmatch(name)
                or any(not _DNS_LABEL.fullmatch(label) for label in name.split("."))):
            raise ValueError("Proposal target is not a valid Kubernetes Deployment name and namespace")
        command = ["kubectl", "--context", self.context, "--namespace", target["namespace"],
                   "get", "deployment", target["name"], "-o", "json"]
        result = self.runner(command, capture_output=True, text=True, timeout=30)
        if result.returncode:
            # kubectl stderr can contain endpoint or credential details; do not propagate it.
            raise RuntimeError(f"Could not read current Deployment (kubectl exit {result.returncode})")
        return {"cluster_id": self.cluster_id, "captured_at": self.clock().isoformat().replace("+00:00", "Z"),
                "deployment": json.loads(result.stdout)}


def _dependencies(store, proposal, now, provided=None):
    stored = store.dependency_result(proposal["action_id"])
    if provided is not None:
        validate_message(provided)
        if provided["kind"] != "dependency_result":
            raise ValueError("Dependency reader must return a dependency_result")
        if any(provided[key] != proposal[key] for key in ("incident_id", "action_id", "target")):
            return provided  # Let Guard reject mismatched input; never persist it.
        if stored:
            provided_at = datetime.fromisoformat(provided["checked_at"].replace("Z", "+00:00"))
            stored_at = datetime.fromisoformat(stored["checked_at"].replace("Z", "+00:00"))
            if provided_at < stored_at:
                return stored
            if provided_at == stored_at:
                if provided != stored:
                    raise CoordinatorError("Dependency reader conflicts with accepted evidence at the same timestamp")
                return stored
        # Persist the exact provider input used by the decision, atomically with
        # its report. This also advances a revoked action's evidence binding.
        record_message(provided, store)
        return provided
    if stored:
        return stored
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
    Readers run before the request's SQLite write transaction, so a slow cluster read
    does not block other workers. The transaction then re-reads the request and
    action and skips anything that changed meanwhile; the next pass re-evaluates it.
    """
    clock = clock or (lambda: datetime.now(timezone.utc))
    pending = store.pending_routes("guard.request")
    decisions = []
    def current(request_id):
        request = store.route(request_id)
        if request["delivered"]:
            return request, None, False
        action = store.action(request["action_id"])
        incident = store.incident(action["incident_id"]) if action else None
        eligible = bool(action and incident["active_action_id"] == action["action_id"]
                        and action["state"] in {"VALIDATING", "AWAITING_APPROVAL"})
        return request, action, eligible

    for pending_row in pending:
        request, action, eligible = current(pending_row[0])
        if request["delivered"]:
            continue
        if eligible:
            proposal = action["proposal"]
            policy = policy_reader(deepcopy(proposal))
            baseline = baseline_reader(deepcopy(proposal)) if baseline_reader else None
            dependencies = dependency_reader(deepcopy(proposal)) if dependency_reader else None
            state = state_reader(deepcopy(proposal))
        with store.transaction():
            request, latest, still_eligible = current(pending_row[0])
            if request["delivered"]:
                continue
            if not still_eligible:
                acknowledge(store, request["id"])
                continue
            if not eligible or latest["proposal"] != proposal:
                continue  # Changed while inputs were read; the next pass re-reads them.
            action = latest
            now = clock()
            if not isinstance(now, datetime) or now.utcoffset() != timedelta(0):
                raise ValueError("Guard worker clock must return timezone-aware UTC")
            dependencies = _dependencies(store, proposal, now, dependencies)
            approval = store.approval(action["action_id"])
            decision, report = evaluate_with_report(
                proposal, state, policy, baseline, dependencies, approval, now=now)
            if decision["verdict"] == "ALLOW":
                assert_decision_usable(decision, proposal, state, policy, baseline,
                                       dependencies, approval, now=now)
            store.save_guard_report(action["action_id"], report)
            record_message(decision, store)
            # Every queued request for this action is covered by the newest
            # evidence read under this same transaction, including a provider
            # finding ingested above. Avoid evaluating superseded requests twice.
            for covered in store.pending_routes("guard.request", action_id=action["action_id"]):
                acknowledge(store, covered[0])
            decisions.append(decision)
    dispatch_pending(store, event_bus)
    return decisions
