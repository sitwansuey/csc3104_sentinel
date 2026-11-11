"""Component-owned inference attempts, shared by local and HTTP workers."""
from copy import deepcopy
import math

from services.guard.common import json_sha256
from services.planner.core import propose
from shared.contracts import validate_message
from shared import log

LOG = log.get_logger("planner")


def escalation(incident):
    message = {"schema_version": "1.0", "kind": "proposal",
        "incident_id": incident["incident_id"], "target": incident["target"],
        "based_on_resource_version": incident["observed_resource_version"],
        "action": "ESCALATE", "parameters": {},
        "rationale": "Planner exhausted its bounded attempts; operator review is required. No repair is authorized.",
        "evidence_refs": [item["evidence_id"] for item in incident["signals"]]}
    message["action_id"] = "ACT-" + json_sha256(message)[:24]
    validate_message(message)
    return message


def prepare(incident, *, journal, baseline_reader, retriever, provider,
            max_attempts=3, retry_seconds=5, lease_seconds=120):
    """Return the exact saved proposal, or None until the next eligible attempt."""
    validate_message(incident)
    if incident["kind"] != "incident":
        raise ValueError("Planner requires an incident")
    if (isinstance(retry_seconds, bool) or not isinstance(retry_seconds, (int, float))
            or not math.isfinite(retry_seconds) or not 0 < retry_seconds <= 3600):
        raise ValueError("Planner retry_seconds must be positive and at most one hour")
    saved = journal.get(incident["incident_id"])
    if saved and saved["incident"] != incident:
        raise ValueError("Planner incident identity reused with changed payload")
    if saved and saved["proposal"] is not None:
        return saved["proposal"]
    claim = journal.claim(incident, max_attempts=max_attempts, lease_seconds=lease_seconds)
    if claim is None:
        return None
    token, exhausted = claim
    error_code = None
    if exhausted:
        error_code, proposal = "ATTEMPTS_EXHAUSTED", escalation(incident)
    else:
        try:
            def recorded_provider(context):
                snapshot = deepcopy(context)
                journal.save_context(incident["incident_id"], token, snapshot)
                try:
                    return provider(context)
                finally:
                    usage = getattr(provider, "usage", None)
                    if isinstance(usage, dict):
                        journal.save_context(incident["incident_id"], token, snapshot, usage)
            proposal = propose(incident, baseline_reader(deepcopy(incident)), retriever, recorded_provider)
        except Exception as error:  # noqa: BLE001 - model and provider errors are retried, then escalated
            attempt = journal.get(incident["incident_id"])["attempts"]
            error_code = "INVALID_PROPOSAL" if isinstance(error, ValueError) else "PROVIDER_UNAVAILABLE"
            log.failure(LOG, "planner.attempt.failed", error, incident_id=incident["incident_id"],
                        attempt=attempt, reason=error_code)
            if attempt < max_attempts:
                journal.retry(incident["incident_id"], token, error_code=error_code,
                              delay_seconds=min(3600, retry_seconds * 2 ** (attempt - 1)))
                return None
            proposal = escalation(incident)
    journal.save(incident["incident_id"], token, proposal, error_code=error_code)
    return proposal
