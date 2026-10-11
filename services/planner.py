"""Owner 3: retrieve runbooks and validate proposals from an injected provider."""

from shared.contracts import validate_message


def propose(incident: dict, baselines: list[dict], retriever, provider) -> dict:
    """Return a typed action with incident evidence; escalate if no baseline exists."""
    validate_message(incident)
    raise NotImplementedError("Implement retrieval, mock-provider call and proposal validation")
