"""Executor action handlers: the exact Pod template each typed action writes.

Pure functions, no I/O. Every implemented action writes through the same
conditional JSON Patch (UID and resourceVersion tests, the full template and
the evidence annotation), so reconciliation in reconciliation.py treats them
alike. A handler only decides which template is written and refuses anything
that does not match the proposal, the trusted baseline and the live template.
"""

from copy import deepcopy

from services.executor.store import fingerprint
from shared.actions import (
    IMPLEMENTED_ACTIONS, config_fingerprint, current_restart_token, restart_template, restart_token,
)
from shared.contracts import ContractError


def _baseline(proposal, baseline):
    """The trusted baseline's template, after checking its identity and hash."""
    if not isinstance(baseline, dict):
        raise ContractError("Stored baseline is unavailable")
    try:
        template = deepcopy(baseline["pod_template"])
        valid = (isinstance(template, dict)
                 and baseline["target"] == proposal["target"]
                 and baseline["baseline_id"] == proposal["parameters"]["baseline_id"]
                 and baseline["template_sha256"] == fingerprint(template))
    except (KeyError, TypeError, ValueError) as exc:
        raise ContractError("Stored baseline identity or hash is invalid") from exc
    if not valid:
        raise ContractError("Stored baseline identity or hash does not match the proposal")
    return template


def _rollback(proposal, live_template, baseline):
    """The saved baseline template, exactly as reviewed."""
    template = _baseline(proposal, baseline)
    if fingerprint(template) != proposal["parameters"]["template_sha256"]:
        raise ContractError("Stored baseline identity or hash does not match the proposal")
    return template


def _restart(proposal, live_template, baseline):
    """The reviewed baseline plus this incident's restart token, and nothing else.

    The live configuration must equal the baseline's, so a RESTART can never
    carry a configuration change, and the live template must not already carry
    the token, so one incident cannot restart twice.
    """
    template = _baseline(proposal, baseline)
    parameters = proposal["parameters"]
    try:
        unchanged = config_fingerprint(live_template) == config_fingerprint(template)
        token = restart_token(proposal["incident_id"], parameters["baseline_id"])
    except (TypeError, ValueError, RecursionError) as exc:
        raise ContractError("Live template is unavailable for a restart") from exc
    if not unchanged:
        raise ContractError("Live configuration differs from the reviewed baseline; a restart cannot change it")
    if parameters["restart_token"] != token:
        raise ContractError("Restart token is not bound to this incident and baseline")
    if current_restart_token(live_template) == token:
        raise ContractError("The live template already carries this restart token")
    candidate = restart_template(template, token)
    if fingerprint(candidate) != parameters["template_sha256"]:
        raise ContractError("Restart template hash does not match the proposal")
    return candidate


HANDLERS = {"ROLLBACK": _rollback, "RESTART": _restart}
assert set(HANDLERS) == IMPLEMENTED_ACTIONS


def implemented(proposal):
    return proposal.get("action") in HANDLERS


def candidate_template(proposal, live_template, baseline):
    """The exact template this action writes; ContractError refuses the write."""
    handler = HANDLERS.get(proposal["action"])
    if handler is None:
        raise ContractError("Action is not implemented by the Executor")
    return handler(proposal, live_template, baseline)


def evidence(proposal, proposal_sha256, attempt_id):
    """The action evidence written with the template.

    A RESTART names its action. A ROLLBACK keeps the five-field form it has
    always written, so annotations written before RESTART existed still match.
    """
    record = {"action_id": proposal["action_id"], "proposal_sha256": proposal_sha256,
              "target_uid": proposal["target"]["uid"],
              "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": attempt_id}
    if proposal["action"] != "ROLLBACK":
        record["action"] = proposal["action"]
    return record
