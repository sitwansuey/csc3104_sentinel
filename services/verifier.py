"""Owner 6: dependency impact before execution; sustained health after execution."""

from shared.contracts import validate_message


def inspect_dependencies(proposal: dict, graph: dict, live_state: dict) -> dict:
    """Traverse caller-to-callee edges; handle cycles, unhealthy and unknown nodes."""
    validate_message(proposal)
    raise NotImplementedError("Implement CLEAR/BLOCKED/UNKNOWN dependency checks")


def verify(execution: dict, samples: list[dict], policy: dict) -> dict:
    """Check window duration, sample gaps and service/dependency health; fail closed."""
    validate_message(execution)
    raise NotImplementedError("Implement RECOVERED/NOT_RECOVERED/UNKNOWN outcomes")
