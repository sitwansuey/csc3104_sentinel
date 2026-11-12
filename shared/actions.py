"""Pure helpers for the typed recovery actions; no I/O and no Kubernetes client.

Every action is a closed, typed record (shared/contracts.py). This module holds
the facts that the Planner, Guard, Executor and contract checks must agree on:
which actions are implemented, and how a RESTART turns a reviewed baseline into
the exact Pod template it writes.

A RESTART adds one annotation, RESTART_ANNOTATION, to the reviewed baseline Pod
template. Kubernetes starts new Pods for any template change, so this is a
rollout restart. It is not `kubectl rollout restart`: the write is the same
conditional JSON Patch as a ROLLBACK (UID and resourceVersion tests plus action
evidence), so reconciliation can prove whether it committed. The configuration
fingerprint ignores only that annotation, so a RESTART can never carry a
configuration change.
"""

from copy import deepcopy
from typing import Any

from shared.canonical import sha256_hex


# Actions whose Guard checks and Executor handler exist. SCALE is in the
# contract but stays refused (ACTION_NOT_IMPLEMENTED) until it has its own.
IMPLEMENTED_ACTIONS = frozenset({"ROLLBACK", "RESTART"})
# Actions that write a reviewed baseline's Pod template (or that template plus a token).
TEMPLATE_ACTIONS = IMPLEMENTED_ACTIONS
RESTART_ANNOTATION = "sentinel.io/restart-token"
RESTART_TOKEN_PREFIX = "RST-"
RESTART_TOKEN_HEX = 16
# The most restart-history entries the Executor supplies and the Guard accepts.
# The ledger returns only the newest ones: a budget allows at most 100 restarts,
# so if every one of them falls in the window the budget is already exhausted.
RESTART_HISTORY_LIMIT = 1000


def restart_token(incident_id: str, baseline_id: str) -> str:
    """The planner-bound token: one per incident and baseline, never chosen by a model."""
    if not isinstance(incident_id, str) or not isinstance(baseline_id, str):
        raise ValueError("Restart token inputs must be strings")
    return RESTART_TOKEN_PREFIX + sha256_hex([incident_id, baseline_id])[:RESTART_TOKEN_HEX]


def _normalised(template: Any) -> dict[str, Any]:
    """A copy without the restart annotation, and without metadata left empty by removing it."""
    if not isinstance(template, dict):
        raise ValueError("Pod template must be an object")
    result = deepcopy(template)
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop(RESTART_ANNOTATION, None)
            if not annotations:
                del metadata["annotations"]
        if not metadata:
            del result["metadata"]
    return result


def config_fingerprint(template: Any) -> str:
    """SHA-256 of the template's configuration: everything except the restart annotation."""
    return sha256_hex(_normalised(template))


def current_restart_token(template: Any) -> str | None:
    """The restart annotation on a template, or None."""
    metadata = template.get("metadata") if isinstance(template, dict) else None
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    value = annotations.get(RESTART_ANNOTATION) if isinstance(annotations, dict) else None
    return value if isinstance(value, str) else None


def restart_template(template: Any, token: str) -> dict[str, Any]:
    """The template with its restart annotation set to token; nothing else changes."""
    if not isinstance(token, str) or not token:
        raise ValueError("Restart token must be a nonempty string")
    result = _normalised(template)
    metadata = result.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("Pod template metadata must be an object")
    annotations = metadata.setdefault("annotations", {})
    if not isinstance(annotations, dict):
        raise ValueError("Pod template annotations must be an object")
    annotations[RESTART_ANNOTATION] = token
    return result


def restart_parameters(incident_id: str, baseline: dict[str, Any]) -> dict[str, str]:
    """The complete RESTART parameters, bound from the incident and a trusted baseline."""
    token = restart_token(incident_id, baseline["baseline_id"])
    return {"baseline_id": baseline["baseline_id"], "restart_token": token,
            "template_sha256": sha256_hex(restart_template(baseline["pod_template"], token))}
