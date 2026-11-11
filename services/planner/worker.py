"""Component-owned inference attempts, shared by local and HTTP workers."""
from copy import deepcopy
import math

from services.guard.common import json_sha256
from services.planner.core import propose
from shared.contracts import validate_message
from shared import log

LOG = log.get_logger("planner")

EXHAUSTED = "Planner exhausted its bounded attempts; operator review is required. No repair is authorized."
# Fixed text for each terminal error code. Exception text never reaches a proposal.
TERMINAL_RATIONALES = {
    "ABSTAINED": "RCA abstained: the evidence does not support a diagnosis. "
                 "Operator review is required. No repair is authorized.",
    "DIAGNOSIS_OFF_TARGET": "Agent diagnosis does not identify the allowed recovery target. "
                            "Operator review is required. No repair is authorized.",
    "TARGET_CHANGED": "The target changed during planning, so the diagnosis is stale. "
                      "Operator review is required. No repair is authorized.",
    "NO_ACTION_PROPOSED": "The Recovery Planner proposed no repair for an observed incident. "
                          "Operator review is required. No repair is authorized.",
}
# Proposal fields that finalize() may change: the live version it binds to, and the
# identity that version is part of. Everything else stays as propose() validated it.
REBINDABLE = {"based_on_resource_version", "action_id"}


class PlanningError(Exception):
    """A classified planning failure. The code is saved in the journal; the text is not.

    A retryable error consumes one bounded attempt. A terminal error ends planning
    at once with the saved ESCALATE for its code.
    """

    def __init__(self, code, *, terminal=False):
        if not isinstance(code, str) or not code.isupper() or not 1 <= len(code) <= 64:
            raise ValueError("Planning error code must be a short upper-case identifier")
        if terminal and code not in TERMINAL_RATIONALES:
            raise ValueError("Terminal planning error has no fixed rationale: " + code)
        super().__init__(code)
        self.code, self.terminal = code, terminal


def escalation(incident, code=None):
    """The saved ESCALATE after exhausted attempts, or for one terminal error code."""
    message = {"schema_version": "1.0", "kind": "proposal",
        "incident_id": incident["incident_id"], "target": incident["target"],
        "based_on_resource_version": incident["observed_resource_version"],
        "action": "ESCALATE", "parameters": {},
        "rationale": EXHAUSTED if code is None else TERMINAL_RATIONALES[code],
        "evidence_refs": [item["evidence_id"] for item in incident["signals"]]}
    message["action_id"] = "ACT-" + json_sha256(message)[:24]
    validate_message(message)
    return message


def diagnosis_summary(diagnosis):
    """What the journal keeps of a diagnosis: no workspace path, report text or raw evidence."""
    prediction = diagnosis.get("prediction") if isinstance(diagnosis, dict) else None
    tools = diagnosis.get("tools") if isinstance(diagnosis, dict) else None
    return {"prediction": prediction if isinstance(prediction, dict) else None,
            "abstained": not prediction,
            "tools": [{key: item.get(key) for key in ("tool", "evidence_id", "success")}
                      for item in tools if isinstance(item, dict)] if isinstance(tools, list) else []}


def _classify(error, agent):
    if isinstance(error, PlanningError):
        return error.code, error.terminal
    if isinstance(error, ValueError):
        # With a diagnoser the provider is a model, so propose() rejected the model's choice.
        return ("INVALID_MODEL_OUTPUT" if agent else "INVALID_PROPOSAL"), False
    return "PROVIDER_UNAVAILABLE", False


def _rebound(proposal, finalize):
    rebound = finalize(deepcopy(proposal))
    validate_message(rebound)
    if ({key: value for key, value in rebound.items() if key not in REBINDABLE}
            != {key: value for key, value in proposal.items() if key not in REBINDABLE}
            or rebound["action_id"] != "ACT-" + json_sha256(
                {key: value for key, value in rebound.items() if key != "action_id"})[:24]):
        raise ValueError("finalize may only rebind the resource version and its action identity")
    return rebound


def prepare(incident, *, journal, baseline_reader, retriever, provider,
            max_attempts=3, retry_seconds=5, lease_seconds=120, diagnoser=None, finalize=None):
    """Return the exact saved proposal, or None until the next eligible attempt.

    diagnoser(incident, attempt=n), when given, diagnoses inside the claimed
    attempt. Its summary is saved as the attempt context, and an abstention ends
    in a saved ESCALATE. finalize(proposal), when given, rebinds a ROLLBACK to
    the live resource version before it is saved, so the rebound proposal is the
    one delivered and replayed. A PlanningError from any step is saved by code.
    """
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
    incident_id = incident["incident_id"]
    error_code = None
    if exhausted:
        error_code, proposal = "ATTEMPTS_EXHAUSTED", escalation(incident)
    else:
        # One context per attempt: the provider context, or a diagnoser's summary.
        recorded = {}

        def usage():
            value = getattr(provider, "usage", None)
            return value if isinstance(value, dict) else None

        try:
            diagnosis = None
            if diagnoser is not None:
                attempt = journal.get(incident_id)["attempts"]
                diagnosis = diagnoser(deepcopy(incident), attempt=attempt)
                recorded["context"] = {"diagnosis": diagnosis_summary(diagnosis)}
                journal.save_context(incident_id, token, recorded["context"], usage())
                if not isinstance(diagnosis, dict) or not diagnosis.get("prediction"):
                    raise PlanningError("ABSTAINED", terminal=True)

            def recorded_provider(context):
                snapshot = recorded.setdefault("context", deepcopy(context))
                journal.save_context(incident_id, token, snapshot)
                try:
                    return provider(context)
                finally:
                    if usage() is not None:
                        journal.save_context(incident_id, token, snapshot, usage())
            proposal = propose(incident, baseline_reader(deepcopy(incident)), retriever, recorded_provider,
                               diagnosis=diagnosis)
            if finalize is not None and proposal["action"] == "ROLLBACK":
                proposal = _rebound(proposal, finalize)
        except Exception as error:  # noqa: BLE001 - model and provider errors are retried, then escalated
            attempt = journal.get(incident_id)["attempts"]
            error_code, terminal = _classify(error, diagnoser is not None)
            log.failure(LOG, "planner.attempt.failed", error, incident_id=incident_id,
                        attempt=attempt, reason=error_code)
            if diagnoser is not None:
                # Keep the attempt's model usage, even when it failed before a context was saved.
                journal.save_context(incident_id, token, recorded.get("context", {"diagnosis": None}), usage())
            if not terminal and attempt < max_attempts:
                journal.retry(incident_id, token, error_code=error_code,
                              delay_seconds=min(3600, retry_seconds * 2 ** (attempt - 1)))
                return None
            proposal = escalation(incident, error_code if terminal else None)
    journal.save(incident_id, token, proposal, error_code=error_code)
    return proposal
