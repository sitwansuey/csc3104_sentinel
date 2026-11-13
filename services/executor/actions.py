"""Executor action handlers: the exact change each typed action writes.

Pure functions, no I/O. ROLLBACK and RESTART write a Pod template through one
conditional JSON Patch (UID and resourceVersion tests, the full template and
the evidence annotation). SCALE (P5.07) writes spec.replicas through the same
tests and evidence, and leaves the template alone. Reconciliation in
reconciliation.py treats them alike. A handler only decides what is written and
refuses anything that does not match the proposal, the trusted baseline and the
live object.
"""

from copy import deepcopy

from services.executor.store import fingerprint
from shared.actions import (
    IMPLEMENTED_ACTIONS, TEMPLATE_ACTIONS, baseline_replicas, config_fingerprint, current_restart_token,
    restart_template, restart_token,
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
assert set(HANDLERS) == TEMPLATE_ACTIONS and TEMPLATE_ACTIONS | {"SCALE"} == IMPLEMENTED_ACTIONS


def implemented(proposal):
    return proposal.get("action") in IMPLEMENTED_ACTIONS


def candidate_template(proposal, live_template, baseline):
    """The exact template a template action writes; ContractError refuses the write."""
    handler = HANDLERS.get(proposal["action"])
    if handler is None:
        raise ContractError("Action does not write a Pod template")
    return handler(proposal, live_template, baseline)


def scale_replicas(proposal, live_deployment, baseline):
    """The replica count a SCALE writes: the reviewed baseline's, and nothing else changes.

    The live configuration must equal the baseline's and the live template must
    be the one the proposal names, so the new Pods run the reviewed template.
    The live count must be below the reviewed one: a SCALE never removes
    replicas and never repeats a count already in place.
    """
    if proposal.get("action") != "SCALE":
        raise ContractError("Action is not a SCALE")
    template = _baseline(proposal, baseline)
    parameters = proposal["parameters"]
    desired = parameters["desired_replicas"]
    if baseline_replicas(baseline) != desired:
        raise ContractError("The requested replica count is not the reviewed baseline's")
    try:
        spec = live_deployment["spec"]
        live_template, current = spec["template"], spec["replicas"]
        unchanged = config_fingerprint(live_template) == config_fingerprint(template)
        bound = fingerprint(live_template) == parameters["template_sha256"]
    except (KeyError, TypeError, ValueError, RecursionError) as exc:
        raise ContractError("Live Deployment is unavailable for a scale") from exc
    if not unchanged:
        raise ContractError("Live configuration differs from the reviewed baseline; scaling up would run it")
    if not bound:
        raise ContractError("Live template is not the one the proposal names")
    if type(current) is not int or current >= desired:
        raise ContractError("The live replica count is not below the reviewed count")
    return desired


def evidence(proposal, proposal_sha256, attempt_id):
    """The action evidence written with the change.

    A RESTART or SCALE names its action; a SCALE also records its replica count,
    and its template_sha256 is the live template it leaves in place. A ROLLBACK
    keeps the five-field form it has always written, so annotations written
    before other actions existed still match.
    """
    record = {"action_id": proposal["action_id"], "proposal_sha256": proposal_sha256,
              "target_uid": proposal["target"]["uid"],
              "template_sha256": proposal["parameters"]["template_sha256"], "attempt_id": attempt_id}
    if proposal["action"] != "ROLLBACK":
        record["action"] = proposal["action"]
    if proposal["action"] == "SCALE":
        record["replicas"] = proposal["parameters"]["desired_replicas"]
    return record
