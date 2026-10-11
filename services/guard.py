"""Owner 4: reject hard violations before considering operator approval."""

from shared.contracts import validate_message


def evaluate(proposal: dict, live_state: dict, policy: dict,
             baseline: dict, dependencies: dict, approval: dict | None = None) -> dict:
    """Check identity, resource version, baseline/hash, dependencies and approval."""
    validate_message(proposal)
    validate_message(dependencies)
    raise NotImplementedError("Implement ALLOW/REJECT/ESCALATE with explicit reason codes")
