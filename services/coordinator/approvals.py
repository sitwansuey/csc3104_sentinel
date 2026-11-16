"""Target-scoped authenticated approval operations; no Kubernetes writes."""
import json
from pathlib import Path

from runtime.service_identity import ProtocolError
from services.coordinator.core import approve
from services.coordinator.store import _digest, _json, _time

OPERATIONS = {"create", "read", "revoke", "history"}
ROLES = {"operator": OPERATIONS, "guard": {"read"}, "executor": {"read"}}
TARGET_FIELDS = {"cluster_id", "namespace", "kind", "name", "uid"}


def load_permissions(path, services):
    """No wildcards: every grant matches the exact stored workload identity."""
    permissions = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(permissions, dict) or not permissions or permissions.keys() - services.keys():
        raise ValueError("Permissions must name configured identities")
    for identity, grants in permissions.items():
        if not isinstance(grants, list) or not grants:
            raise ValueError("Expected nonempty target grants")
        for grant in grants:
            if (not isinstance(grant, dict) or set(grant) != {"target", "operations"}
                    or not isinstance(grant["target"], dict) or set(grant["target"]) != TARGET_FIELDS
                    or any(not isinstance(value, str) or not value.strip() or value == "*"
                           for value in grant["target"].values())
                    or not isinstance(grant["operations"], list) or not grant["operations"]
                    or any(not isinstance(op, str) or op not in ROLES[services[identity][0]]
                           for op in grant["operations"])):
                raise ValueError("Invalid target or role-specific approval permissions")
    return permissions


class ApprovalAccess:
    def __init__(self, store, permissions, *, max_lifetime_seconds=300):
        self.store, self.permissions = store, permissions
        self.max_lifetime_seconds = max_lifetime_seconds

    def _action(self, identity, role, operation, body):
        action = self.store.action(body["action_id"])
        if (operation not in ROLES.get(role, set()) or action is None
                or not any(operation in grant["operations"] and grant["target"] == action["proposal"]["target"]
                           for grant in self.permissions.get(identity, []))):
            raise ProtocolError(403, "Approval access denied")
        if body["proposal_sha256"] != _digest(action["proposal"]):
            raise ProtocolError(409, "Proposal precondition changed")
        return action

    def call(self, identity, role, operation, body):
        required = {"action_id", "proposal_sha256"}
        required |= {"policy_version", "expires_at"} if operation == "create" else set()
        required |= {"approval_id", "reason"} if operation == "revoke" else set()
        if operation not in OPERATIONS:
            raise ProtocolError(404, "Unknown approval operation")
        if (not isinstance(body, dict) or set(body) != required
                or any(not isinstance(body[key], str) or not body[key].strip()
                       for key in required - {"approval_id"})
                or "approval_id" in body and (type(body["approval_id"]) is not int or body["approval_id"] <= 0)
                or len(body.get("reason", "")) > 1000):
            raise ProtocolError(400, "Invalid approval request")
        if operation == "create":
            self._action(identity, role, operation, body)
            lifetime = (_time(body["expires_at"]) - _time(self.store.now())).total_seconds()
            if not 0 < lifetime <= self.max_lifetime_seconds:
                raise ProtocolError(400, "Approval expiry exceeds the allowed lifetime")
            record = approve(body["action_id"], identity, self.store, expires_at=body["expires_at"],
                             expected_proposal_sha256=body["proposal_sha256"],
                             expected_policy_version=body["policy_version"], managed=True)
            row = self.store.database.execute("SELECT id FROM approvals WHERE action_id=? AND record=? ORDER BY id LIMIT 1",
                                              (body["action_id"], _json(record))).fetchone()
            return {"approval_id": row["id"], "approval": record}
        with self.store.transaction():
            action = self._action(identity, role, operation, body)
            if operation == "read":
                return {"approval": self.store.current_approval(body["action_id"], managed_only=True)}
            if operation == "history":
                return {"history": self.store.approval_history(body["action_id"])}
            row = self.store.approval_row(body["action_id"])
            if row is None or row["id"] != body["approval_id"]:
                raise ProtocolError(409, "Revocation must name the latest approval")
            previous = self.store.database.execute("SELECT record FROM approval_revocations WHERE approval_id=?",
                                                   (row["id"],)).fetchone()
            if previous:
                record = json.loads(previous[0])
                if record["operator_id"] != identity or record["reason"] != body["reason"]:
                    raise ProtocolError(409, "Revocation retry changed")
                return {"revocation": record}
            if action["state"] not in {"VALIDATING", "AWAITING_APPROVAL", "READY"}:
                raise ProtocolError(409, "Execution already reported; revocation cannot undo a write")
            record = {"approval_id": row["id"], "action_id": body["action_id"], "operator_id": identity,
                      "revoked_at": self.store.now(), "reason": body["reason"]}
            self.store.database.execute("INSERT INTO approval_revocations VALUES(?,?)", (row["id"], _json(record)))
            self.store.set_state(action["incident_id"], body["action_id"], "VALIDATING", cause="revocation")
            self.store.enqueue("guard.request", {"proposal": action["proposal"], "approval": None}, body["action_id"])
            return {"revocation": record}
