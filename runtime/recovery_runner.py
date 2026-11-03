"""Advance one observed incident through durable component handoffs."""
from services.coordinator import approve


def advance(store, incident_id, *, plan, assess, guard, operator, execute, verify):
    def state():
        return store.incident(incident_id)["state"]

    incident = store.incident(incident_id)
    if incident is None:
        raise ValueError("Observer incident has not been delivered")
    if state() == "OPEN":
        plan()
    action_id = store.incident(incident_id)["active_action_id"]
    if not action_id:
        raise ValueError("Planner has not delivered a proposal for this incident")
    proposal = store.action(action_id)["proposal"]
    if proposal["action"] != "ROLLBACK":
        raise ValueError("Recovery requires a supported rollback proposal")

    if state() == "VALIDATING":
        assess(proposal)
        guard()
    if state() == "AWAITING_APPROVAL":
        decision = store.action(action_id)["guard_result"]
        if decision["reason_codes"] == ["OPERATOR_APPROVAL_REQUIRED"]:
            identity = operator(proposal)
            if identity:
                approve(action_id, identity, store)
                assess(proposal)
                guard()
    if state() in {"READY", "NEEDS_RECONCILIATION"}:
        execute(proposal)
    if state() == "VERIFYING":
        verify(proposal, store.action(action_id)["execution_result"])
    return state()

