"""Advance one observed incident through durable component handoffs."""
from services.coordinator.core import approve as _local_approve


def legacy_local_approve(store, operator):
    """Trusted local, offline demo approval only; it authenticates nobody.

    operator(proposal) returns an identity or None. The approval is written with
    the Coordinator's direct approve(), which the authenticated lookup never
    promotes. Live harnesses inject an approver from runtime.approval_session.
    """
    def approve(proposal, decision):
        identity = operator(proposal)
        return _local_approve(proposal["action_id"], identity, store) if identity else None
    return approve


def advance(store, incident_id, *, plan, assess, guard, approve, execute, verify):
    """approve(proposal, guard_decision) records an approval and returns it, or None to refuse."""
    def state():
        return store.incident(incident_id)["state"]

    incident = store.incident(incident_id)
    if incident is None:
        raise ValueError("Observer incident has not been delivered")
    if state() == "OPEN":
        plan()
    action_id = store.incident(incident_id)["active_action_id"]
    if not action_id:
        if state() == "OPEN":
            return "OPEN"  # Durable Planner retry/backoff is still pending.
        raise ValueError("Planner has not delivered a proposal for this incident")
    proposal = store.action(action_id)["proposal"]
    if proposal["action"] == "ESCALATE":
        if state() == "VALIDATING":
            guard()
        return state()
    if proposal["action"] not in ("ROLLBACK", "RESTART", "SCALE"):
        raise ValueError("Recovery requires a supported rollback, restart or scale proposal")

    if state() == "VALIDATING":
        assess(proposal)
        guard()
    if state() == "AWAITING_APPROVAL":
        decision = store.action(action_id)["guard_result"]
        if decision["reason_codes"] == ["OPERATOR_APPROVAL_REQUIRED"]:
            if approve(proposal, decision):
                assess(proposal)
                guard()
    if state() in {"READY", "NEEDS_RECONCILIATION"}:
        execute(proposal)
    if state() == "VERIFYING":
        verify(proposal, store.action(action_id)["execution_result"])
    return state()
