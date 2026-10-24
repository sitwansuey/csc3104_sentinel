"""Offline Recovery Guard: hard violations take precedence over approval.

This evaluator performs no I/O. Its baseline, policy and approval inputs must
come from trusted stores when live adapters replace the development fixtures.
See docs/recovery-guard.md for the temporary local input contracts.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

from shared.contracts import ACTION_TYPES, ContractError, validate_message
from services.guard_common import (
    GuardInputError, mapping as _mapping, string as _string,
    utc_timestamp as _utc_timestamp, json_sha256 as _json_sha256,
)


TARGET_FIELDS = ("cluster_id", "namespace", "kind", "name", "uid")


def _target(value, path):
    _mapping(value, path)
    if set(value) != set(TARGET_FIELDS):
        raise GuardInputError(f"{path}: expected the full target identity")
    for key in TARGET_FIELDS:
        _string(value[key], f"{path}.{key}")
    if value["kind"] != "Deployment":
        raise GuardInputError(f"{path}.kind: only Deployment is supported")


def template_sha256(pod_template: dict) -> str:
    """Hash the exact template using the starter's canonical JSON encoding."""
    _mapping(pod_template, "pod_template")
    return _json_sha256(pod_template)


def proposal_fingerprint(proposal: dict) -> str:
    """Bind approval to every field of a schema-valid proposal."""
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise GuardInputError("proposal.kind: expected proposal")
    return _json_sha256(proposal)


def _validate_policy(policy):
    _mapping(policy, "policy")
    for key in ("policy_id", "policy_version"):
        _string(policy.get(key), f"policy.{key}")
    targets = policy.get("targets")
    if not isinstance(targets, list):
        raise GuardInputError("policy.targets: expected a list")
    for index, target in enumerate(targets):
        _target(target, f"policy.targets[{index}]")
    actions = policy.get("allowed_actions")
    if not isinstance(actions, list) or any(
        not isinstance(action, str) or action not in ACTION_TYPES for action in actions
    ):
        raise GuardInputError("policy.allowed_actions: expected a list of typed actions")
    if type(policy.get("require_operator_approval")) is not bool:
        raise GuardInputError("policy.require_operator_approval: expected a boolean")
    if policy.get("unknown_dependency_result") not in ("REJECT", "ESCALATE"):
        raise GuardInputError("policy.unknown_dependency_result: expected REJECT or ESCALATE")
    for key in ("max_state_age_seconds", "max_dependency_age_seconds"):
        if key in policy and (type(policy[key]) is not int or policy[key] < 0):
            raise GuardInputError(f"policy.{key}: expected a nonnegative integer")
    return None  # Extended/resource policy validation is owned by guard_decisions.


def _live_snapshot(live_state):
    _mapping(live_state, "live_state")
    cluster = _string(live_state.get("cluster_id"), "live_state.cluster_id")
    captured_at = _utc_timestamp(live_state.get("captured_at"), "live_state.captured_at")
    deployment = _mapping(live_state.get("deployment"), "live_state.deployment")
    kind = _string(deployment.get("kind"), "live_state.deployment.kind")
    metadata = _mapping(deployment.get("metadata"), "live_state.deployment.metadata")
    for key in ("namespace", "name", "uid", "resourceVersion"):
        _string(metadata.get(key), f"live_state.deployment.metadata.{key}")
    target = {
        "cluster_id": cluster, "kind": kind,
        "namespace": metadata["namespace"], "name": metadata["name"],
        "uid": metadata["uid"],
    }
    return target, metadata["resourceVersion"], captured_at


def _baseline_reasons(proposal, baseline, checked_at):
    if baseline is None:
        return ["BASELINE_MISSING"]
    try:
        _mapping(baseline, "baseline")
        _target(baseline.get("target"), "baseline.target")
        _string(baseline.get("baseline_id"), "baseline.baseline_id")
        _string(baseline.get("template_sha256"), "baseline.template_sha256")
        healthy_at = _utc_timestamp(baseline.get("healthy_observed_at"), "baseline.healthy_observed_at")
        template = _mapping(baseline.get("pod_template"), "baseline.pod_template")
        # Full Kubernetes validation belongs to the baseline store/API adapter.
        if not isinstance(template.get("spec"), dict) or not isinstance(
            template["spec"].get("containers"), list
        ) or not template["spec"]["containers"]:
            raise GuardInputError("baseline.pod_template: expected a container specification")
        digest = template_sha256(template)
    except GuardInputError:
        return ["INVALID_BASELINE"]
    reasons = []
    if baseline["target"] != proposal["target"]:
        reasons.append("BASELINE_TARGET_MISMATCH")
    if baseline["baseline_id"] != proposal["parameters"]["baseline_id"]:
        reasons.append("BASELINE_ID_MISMATCH")
    if digest != baseline["template_sha256"] or digest != proposal["parameters"]["template_sha256"]:
        reasons.append("BASELINE_HASH_MISMATCH")
    if healthy_at > checked_at:
        reasons.append("BASELINE_FROM_FUTURE")
    return reasons


def _approval_reasons(proposal, policy, approval, checked_at):
    """Return (hard rejection reasons, escalation reasons)."""
    if approval is None:
        return [], ["OPERATOR_APPROVAL_REQUIRED"] if policy["require_operator_approval"] else []
    required = {"action_id", "proposal_sha256", "policy_version", "operator_id", "decision", "approved_at"}
    try:
        _mapping(approval, "approval")
        if not required <= approval.keys() or approval.keys() - required - {"expires_at"}:
            raise GuardInputError("approval: unexpected or missing fields")
        for key in required:
            _string(approval[key], f"approval.{key}")
        if not re.fullmatch(r"[a-f0-9]{64}", approval["proposal_sha256"]):
            raise GuardInputError("approval.proposal_sha256: expected a lowercase SHA-256 digest")
        if approval["decision"] not in ("APPROVED", "REJECTED"):
            raise GuardInputError("approval.decision: expected APPROVED or REJECTED")
        approved_at = _utc_timestamp(approval["approved_at"], "approval.approved_at")
        expires_at = _utc_timestamp(approval["expires_at"], "approval.expires_at") if "expires_at" in approval else None
        if expires_at is not None and expires_at < approved_at:
            raise GuardInputError("approval.expires_at: precedes approved_at")
    except GuardInputError:
        return [], ["INVALID_APPROVAL"]
    if (
        approval["action_id"] != proposal["action_id"]
        or approval["proposal_sha256"] != proposal_fingerprint(proposal)
        or approval["policy_version"] != policy["policy_version"]
    ):
        return [], ["APPROVAL_MISMATCH"]
    if approved_at > checked_at:
        return [], ["APPROVAL_FROM_FUTURE"]
    if expires_at is not None and checked_at >= expires_at:
        return [], ["APPROVAL_EXPIRED"]
    if approval["decision"] == "REJECTED":
        return ["OPERATOR_REJECTED"], []
    return [], []


def _evaluate_core(proposal: dict, live_state: dict, policy: dict,
             baseline: dict | None, dependencies: dict, approval: dict | None = None,
             *, now: datetime | None = None, _trace=None) -> dict:
    """Validate one explicit rollback without performing Kubernetes writes.

    Malformed wire inputs raise ContractError. Invalid policy/live-state inputs
    raise GuardInputError; no resource version is invented for unavailable state.
    Inject a UTC `now` for reproducible fixture tests and freshness decisions.
    """
    validate_message(proposal)
    validate_message(dependencies)
    if proposal["kind"] != "proposal" or dependencies["kind"] != "dependency_result":
        raise GuardInputError("Expected proposal and dependency_result message kinds")
    _validate_policy(policy)
    live_target, resource_version, captured_at = _live_snapshot(live_state)
    checked_at = datetime.now(timezone.utc) if now is None else now
    if not isinstance(checked_at, datetime) or checked_at.utcoffset() != timedelta(0):
        raise GuardInputError("now: expected a timezone-aware UTC datetime")

    rejected, escalated = [], []
    if proposal["target"] not in policy["targets"]:
        rejected.append("TARGET_NOT_ALLOWED")
    if proposal["action"] not in policy["allowed_actions"]:
        rejected.append("ACTION_NOT_ALLOWED")
    elif proposal["action"] != "ROLLBACK":
        rejected.append("ACTION_NOT_IMPLEMENTED")
    if any(live_target[key] != proposal["target"][key] for key in TARGET_FIELDS if key != "uid"):
        rejected.append("LIVE_TARGET_MISMATCH")
    if live_target["uid"] != proposal["target"]["uid"]:
        rejected.append("TARGET_UID_MISMATCH")
    if proposal["based_on_resource_version"] != resource_version:
        rejected.append("STALE_RESOURCE_VERSION")
    if captured_at > checked_at:
        rejected.append("LIVE_STATE_FROM_FUTURE")
    elif "max_state_age_seconds" in policy and (checked_at - captured_at).total_seconds() > policy["max_state_age_seconds"]:
        rejected.append("STALE_LIVE_STATE")
    if proposal["action"] == "ROLLBACK":
        rejected.extend(_baseline_reasons(proposal, baseline, checked_at))

    dependency_unknowns = []
    if any(dependencies[key] != proposal[key] for key in ("incident_id", "action_id", "target")):
        rejected.append("DEPENDENCY_RESULT_MISMATCH")
    if dependencies["status"] == "BLOCKED" or dependencies["blockers"]:
        rejected.append("DEPENDENCY_BLOCKED")
    if dependencies["status"] == "UNKNOWN" or dependencies["unknowns"]:
        dependency_unknowns.append("DEPENDENCY_UNKNOWN")
    dependency_at = _utc_timestamp(dependencies["checked_at"], "dependencies.checked_at")
    if dependency_at > checked_at:
        dependency_unknowns.append("DEPENDENCY_RESULT_FROM_FUTURE")
    elif "max_dependency_age_seconds" in policy and (checked_at - dependency_at).total_seconds() > policy["max_dependency_age_seconds"]:
        dependency_unknowns.append("STALE_DEPENDENCY_RESULT")
    if policy["unknown_dependency_result"] == "REJECT":
        rejected.extend(dependency_unknowns)
    else:
        escalated.extend(dependency_unknowns)

    # Approval cannot rescue any of the preceding hard failures.
    approval_checked = not rejected
    if approval_checked:
        approval_rejected, approval_escalated = _approval_reasons(proposal, policy, approval, checked_at)
        rejected.extend(approval_rejected)
        escalated.extend(approval_escalated)
    if _trace is not None:
        _trace.update(rejected=list(rejected), escalated=list(escalated),
                      approval_checked=approval_checked)
    verdict = "REJECT" if rejected else "ESCALATE" if escalated else "ALLOW"
    result = {
        "schema_version": "1.0", "kind": "guard_result",
        "incident_id": proposal["incident_id"], "action_id": proposal["action_id"],
        "target": deepcopy(proposal["target"]),
        "verdict": verdict,
        "reason_codes": rejected or escalated or ["VALIDATION_PASSED"],
        "policy_version": policy["policy_version"],
        "validated_resource_version": resource_version,
        "checked_at": checked_at.isoformat().replace("+00:00", "Z"),
    }
    validate_message(result)
    return result


def evaluate_with_report(proposal, live_state, policy, baseline, dependencies,
                         approval=None, *, now=None, preview=None):
    """Evaluate current inputs and return (version-2 decision, bounded audit report)."""
    from services.guard_decisions import evaluate_with_report as enhanced
    return enhanced(_evaluate_core, proposal, live_state, policy, baseline,
                    dependencies, approval, now=now, preview=preview)


def evaluate(proposal, live_state, policy, baseline, dependencies,
             approval=None, *, now=None, preview=None):
    """Return an exact-input-bound, expiring decision without applying recovery."""
    return evaluate_with_report(proposal, live_state, policy, baseline,
                                dependencies, approval, now=now, preview=preview)[0]


def assert_decision_usable(result, proposal, live_state, policy, baseline,
                           dependencies, approval=None, *, now=None, preview=None):
    """Check a trusted response's binding/expiry; still require a conditional write."""
    from services.guard_decisions import assert_decision_usable as check
    return check(result, proposal, live_state, policy, baseline, dependencies,
                 approval, now=now, preview=preview)
