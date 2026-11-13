"""Pending approvals shown in the lab dashboard, decided without a browser token.

The harness's dashboard approver (`--approval ui`) offers each escalated
proposal here and waits. The dashboard server lists the code-derived facts in
its snapshot and relays an operator's decision to the run's approval authority
with the operator credential the harness holds in memory. The browser
authenticates to the dashboard only with the per-session lab token. It never
receives or sends an approval token, and a request cannot choose the authority
URL, the operator identity, the approval expiry or the revocation reason.

A decision needs the exact action ID typed back, the proposal digest the page
was showing, and, to revoke, the latest approval ID. The approval authority
still checks the stored proposal, the policy version and the lifecycle state.
"""
from datetime import UTC, datetime, timedelta
import threading
import time

from runtime.service_identity import ProtocolError
from services.coordinator.store import _digest

PENDING, DECIDING, APPROVED, REVOKING, REVOKED, EXPIRED, CLOSED = (
    "PENDING", "DECIDING", "APPROVED", "REVOKING", "REVOKED", "EXPIRED", "CLOSED")
MODE = "ui"
RATIONALE_LIMIT = 1000
FIELD_LIMIT = 160
TEXT_LIMIT = 200
KEEP = 20
REVOKE_REASON = "Revoked by the operator in the dashboard approval panel"
APPROVE_FIELDS = frozenset({"action_id", "proposal_sha256", "confirm_action_id"})
REVOKE_FIELDS = APPROVE_FIELDS | {"approval_id"}
# Fixed messages: the authority's own error text never reaches the page.
UPSTREAM = {
    400: "The approval authority refused the request as invalid.",
    401: "The approval authority did not accept the dashboard operator credential.",
    403: "The dashboard operator is not permitted to decide this action.",
    409: "The approval authority refused: the proposal or the action's state changed. Refresh before retrying.",
}
REVOKE_CONFLICT = ("The approval authority refused the revocation: execution has already been reported, "
                   "so revoking cannot undo a write.")
# Failures of the authenticated call itself; anything else is a bug and propagates.
UPSTREAM_ERRORS = (ProtocolError, OSError, ValueError, KeyError, TypeError)


class RelayRefused(Exception):
    """A decision the relay did not pass on; status and message are safe to show."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def _utc(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _text(value, limit):
    text = value if isinstance(value, str) else repr(value)
    cleaned = "".join(char if char.isprintable() else " " for char in text)
    return cleaned[:limit]


class ApprovalRelay:
    """Shared between one harness approver thread and the dashboard's HTTP threads."""

    def __init__(self, *, clock=None, monotonic=None):
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self._condition = threading.Condition()
        self._requests = {}
        self._closed = False

    def offer(self, approver, proposal, decision, *, wait_seconds):
        """Show one escalated proposal and wait for the dashboard decision.

        Returns the created approval record, or None when the request expires
        or the dashboard stops first. Called on the harness thread.
        """
        if not 0 < wait_seconds <= 3600:
            raise ValueError("Dashboard approval wait must be positive and at most 1 hour")
        action_id = proposal["action_id"]
        requested = self.clock()
        entry = {"approver": approver, "proposal": proposal, "decision": decision,
                 "digest": _digest(proposal), "status": PENDING, "record": None, "approval_id": None,
                 "revocation": None, "requested_at": _utc(requested),
                 "expires_at": _utc(requested + timedelta(seconds=wait_seconds)),
                 "deadline": self.monotonic() + wait_seconds}
        with self._condition:
            if self._closed:
                return None
            current = self._requests.get(action_id)
            if current is not None and current["status"] in {PENDING, DECIDING, REVOKING}:
                raise RuntimeError("This action already has an open dashboard request")
            self._requests.pop(action_id, None)
            self._requests[action_id] = entry
            for stale in [key for key, item in self._requests.items()
                          if item["status"] not in {PENDING, DECIDING, REVOKING}][:-(KEEP - 1)]:
                del self._requests[stale]
            self._condition.notify_all()
            try:
                while True:
                    if entry["status"] == APPROVED:
                        return entry["record"]
                    if entry["status"] != DECIDING:  # An approval already on its way upstream is awaited.
                        if self._closed:
                            entry["status"] = CLOSED
                            return None
                        if self.monotonic() >= entry["deadline"]:
                            entry["status"] = EXPIRED
                            return None
                    self._condition.wait(timeout=.25)
            except BaseException:
                if entry["status"] == PENDING:  # Interrupted: nobody is waiting for this decision any more.
                    entry["status"] = CLOSED
                raise

    def requests(self):
        """Public projection for the dashboard snapshot: facts only, never a credential."""
        with self._condition:
            for entry in self._requests.values():
                if entry["status"] == PENDING and self.monotonic() >= entry["deadline"]:
                    entry["status"] = EXPIRED
            return [self._public(entry) for entry in reversed(self._requests.values())]

    @staticmethod
    def _public(entry):
        proposal, decision = entry["proposal"], entry["decision"]
        parameters = proposal.get("parameters") if isinstance(proposal.get("parameters"), dict) else {}
        rationale = proposal.get("rationale", "")
        rationale = rationale if isinstance(rationale, str) else repr(rationale)
        record = entry["record"]
        public = {
            "action_id": _text(proposal["action_id"], TEXT_LIMIT),
            "incident_id": _text(proposal.get("incident_id", ""), TEXT_LIMIT),
            "status": entry["status"], "mode": MODE,
            "operator_id": _text(entry["approver"].identity or "", TEXT_LIMIT),
            "target": {key: _text(value, FIELD_LIMIT) for key, value in proposal["target"].items()},
            "action": _text(proposal.get("action", ""), FIELD_LIMIT),
            "baseline_id": _text(str(parameters.get("baseline_id", "")), FIELD_LIMIT),
            "template_sha256_prefix": _text(str(parameters.get("template_sha256", ""))[:12], 12),
            "proposal_sha256": entry["digest"],
            "policy_version": _text(str(decision.get("policy_version", "")), FIELD_LIMIT),
            "guard": {"verdict": _text(decision.get("verdict", ""), FIELD_LIMIT),
                      "reason_codes": [_text(code, FIELD_LIMIT) for code in decision.get("reason_codes", [])][:20]},
            "rationale": _text(rationale, RATIONALE_LIMIT),
            "rationale_truncated": len(rationale) > RATIONALE_LIMIT,
            "requested_at": entry["requested_at"], "expires_at": entry["expires_at"],
            "approval": None if record is None else {
                "approval_id": entry["approval_id"], "operator_id": record["operator_id"],
                "approved_at": record["approved_at"], "expires_at": record["expires_at"]},
            "revocation": entry["revocation"],
        }
        if proposal.get("action") == "SCALE":
            # P5.07: the replica count a SCALE would write (null if malformed); other actions omit the key.
            replicas = parameters.get("desired_replicas")
            public["desired_replicas"] = replicas if type(replicas) is int else None
        return public

    @staticmethod
    def _fields(payload, expected):
        if (not isinstance(payload, dict) or set(payload) != expected
                or any(not isinstance(payload[key], str) or not 0 < len(payload[key]) <= TEXT_LIMIT
                       for key in expected - {"approval_id"})
                or "approval_id" in expected
                and (type(payload["approval_id"]) is not int or payload["approval_id"] <= 0)):
            raise RelayRefused(400, "Invalid approval request.")
        return payload

    def _entry(self, payload):
        entry = self._requests.get(payload["action_id"])
        if self._closed or entry is None:
            raise RelayRefused(404, "No dashboard approval request exists for this action.")
        if entry["status"] == PENDING and self.monotonic() >= entry["deadline"]:
            entry["status"] = EXPIRED
        if payload["proposal_sha256"] != entry["digest"]:
            raise RelayRefused(409, "The proposal changed. Refresh before deciding.")
        if payload["confirm_action_id"] != payload["action_id"]:
            raise RelayRefused(400, "The typed action ID does not match this action.")
        return entry

    def approve(self, payload):
        """Relay one typed approval; called on a dashboard HTTP thread."""
        payload = self._fields(payload, APPROVE_FIELDS)
        with self._condition:
            entry = self._entry(payload)
            if entry["status"] == EXPIRED:
                raise RelayRefused(410, "This approval request expired. No approval was recorded.")
            if entry["status"] != PENDING:
                raise RelayRefused(409, "This action is no longer waiting for approval.")
            entry["status"] = DECIDING
        try:
            try:
                record, approval_id = entry["approver"].approve_offer(entry["proposal"], entry["decision"])
            except BaseException:
                with self._condition:
                    entry["status"] = PENDING
                    self._condition.notify_all()
                raise
        except UPSTREAM_ERRORS as error:
            raise _refusal(error, "approve") from None
        with self._condition:
            entry.update(status=APPROVED, record=record, approval_id=approval_id)
            self._condition.notify_all()
            return {**self._public(entry)["approval"], "action_id": entry["proposal"]["action_id"], "mode": MODE}

    def revoke(self, payload):
        """Relay a revocation of the latest approval; the authority refuses after execution is reported."""
        payload = self._fields(payload, REVOKE_FIELDS)
        with self._condition:
            entry = self._entry(payload)
            if entry["status"] == REVOKED:
                raise RelayRefused(409, "This approval is already revoked.")
            if entry["status"] != APPROVED:
                raise RelayRefused(409, "There is no approval to revoke for this action.")
            if payload["approval_id"] != entry["approval_id"]:
                raise RelayRefused(409, "Revocation must name the latest approval. Refresh before retrying.")
            if self.clock() >= _instant(entry["record"]["expires_at"]):
                raise RelayRefused(410, "This approval already expired and cannot authorise a write.")
            entry["status"] = REVOKING
        try:
            try:
                revocation = entry["approver"].revoke_offer(entry["proposal"], entry["approval_id"], REVOKE_REASON)
            except BaseException:
                with self._condition:
                    entry["status"] = APPROVED
                    self._condition.notify_all()
                raise
        except UPSTREAM_ERRORS as error:
            raise _refusal(error, "revoke") from None
        with self._condition:
            entry.update(status=REVOKED, revocation={"operator_id": revocation["operator_id"],
                                                     "revoked_at": revocation["revoked_at"]})
            self._condition.notify_all()
            return {"action_id": entry["proposal"]["action_id"], "approval_id": entry["approval_id"],
                    "mode": MODE, **entry["revocation"]}

    def close(self):
        """Stop offering decisions and release a waiting approver."""
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def _refusal(error, operation):
    """Map an upstream failure to a fixed, credential-free refusal."""
    if isinstance(error, ProtocolError):
        if operation == "revoke" and error.status == 409:
            return RelayRefused(409, REVOKE_CONFLICT)
        status = error.status if error.status in UPSTREAM else 502
        return RelayRefused(status, UPSTREAM.get(status, "The approval authority failed. Nothing was decided."))
    return RelayRefused(503, "The approval authority is unavailable. Nothing was decided.")
