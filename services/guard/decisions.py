"""Versioned Guard decisions, strict live policy and explanatory audit records.

Digests bind inputs; they do not authenticate a caller or replace execution locks.
"""

from datetime import datetime, timedelta, timezone

from services.guard import (
    eligibility as guard_eligibility, resources as guard_resources, restart as guard_restart,
)
from services.guard.common import GuardInputError, finding, json_sha256, mapping, utc_timestamp
from shared.actions import TEMPLATE_ACTIONS, config_fingerprint
from shared.contracts import validate_message


_MAX_AGE_SECONDS = 366 * 24 * 60 * 60


def _integer(policy, key, *, required=False, maximum=None):
    if key not in policy:
        if required:
            raise GuardInputError(f"policy.{key}: required for live use")
        return
    value = policy[key]
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise GuardInputError(f"policy.{key}: expected a positive bounded integer")


def validate_policy(policy, *, require_live=False):
    mapping(policy, "policy")
    profile = policy.get("profile", "development")
    if profile not in ("development", "live") or (require_live and profile != "live"):
        raise GuardInputError("A reviewed live policy profile is required for live reads")
    _integer(policy, "decision_ttl_seconds", required=profile == "live", maximum=300)
    _integer(policy, "max_approval_lifetime_seconds", required=profile == "live", maximum=_MAX_AGE_SECONDS)
    for key in ("max_state_age_seconds", "max_dependency_age_seconds"):
        if key in policy and (type(policy[key]) is not int or not 0 <= policy[key] <= _MAX_AGE_SECONDS):
            raise GuardInputError(f"policy.{key}: expected a bounded nonnegative age")
    if "require_server_dry_run" in policy and type(policy["require_server_dry_run"]) is not bool:
        raise GuardInputError("policy.require_server_dry_run: expected boolean")
    _integer(policy, "max_server_dry_run_age_seconds", required=policy.get("require_server_dry_run", False), maximum=300)
    guard_eligibility.validate_policy(policy)
    guard_resources.validate_policy(policy)
    guard_restart.validate_policy(policy)
    for key in ("max_age_seconds", "min_healthy_duration_seconds"):
        if key in policy.get("baseline_constraints", {}) and policy["baseline_constraints"][key] > _MAX_AGE_SECONDS:
            raise GuardInputError(f"policy.baseline_constraints.{key}: duration exceeds supported bound")
    if profile != "live":
        return
    for key in ("max_state_age_seconds", "max_dependency_age_seconds"):
        _integer(policy, key, required=True)
    if policy.get("require_operator_approval") is not True:
        raise GuardInputError("Live profile requires operator approval")
    baseline = mapping(policy.get("baseline_constraints"), "policy.baseline_constraints")
    for key in ("require_health_evidence", "require_compatible_references", "require_image_digests"):
        if baseline.get(key) is not True:
            raise GuardInputError(f"Live baseline policy requires {key}")
    for key in ("max_age_seconds", "min_healthy_duration_seconds"):
        _integer(baseline, key, required=True)
    if not baseline.get("trusted_sources"):
        raise GuardInputError("Live profile requires trusted health sources")
    security = mapping(policy.get("security_constraints"), "policy.security_constraints")
    for key in ("deny_privileged", "deny_host_access", "forbid_service_account_change"):
        if security.get(key) is not True:
            raise GuardInputError(f"Live security policy requires {key}")
    for key in ("allowed_image_registries", "allowed_service_accounts"):
        if not security.get(key):
            raise GuardInputError(f"Live security policy requires {key}")
    resources = mapping(policy.get("resource_constraints"), "policy.resource_constraints")
    if resources.get("require_requests_and_limits") is not True:
        raise GuardInputError("Live profile requires explicit container resources")
    workload = mapping(policy.get("workload_constraints"), "policy.workload_constraints")
    for key in ("max_pod_requests", "max_rollout_requests"):
        if set(mapping(workload.get(key), f"policy.workload_constraints.{key}")) != {"cpu", "memory"}:
            raise GuardInputError(f"Live profile requires CPU and memory in {key}")
    for key in ("max_replicas", "max_surge_replicas"):
        if key not in workload:
            raise GuardInputError(f"Live profile requires {key}")


def input_fingerprint(proposal, live_state, policy, baseline, dependencies, approval=None, preview=None):
    return json_sha256({
        "proposal": proposal, "live_state": live_state, "policy": policy,
        "baseline": baseline, "dependencies": dependencies,
        "approval": approval, "preview": preview,
    })


def _clock(now):
    value = datetime.now(timezone.utc) if now is None else now
    if not isinstance(value, datetime) or value.utcoffset() != timedelta(0):
        raise GuardInputError("now: expected timezone-aware UTC datetime")
    return value


def _approval_checks(policy, approval, checked_at):
    if approval is None or not isinstance(approval, dict):
        return []  # Core check already decides missing/malformed approval.
    if policy.get("profile") != "live" and "max_approval_lifetime_seconds" not in policy:
        return []
    if "expires_at" not in approval:
        return [finding("APPROVAL_EXPIRY_REQUIRED", "approval.expires_at", None,
                        "bounded expiry", "A live approval must have an expiry.", "UNKNOWN")]
    try:
        start = utc_timestamp(approval.get("approved_at"), "approval.approved_at")
        end = utc_timestamp(approval.get("expires_at"), "approval.expires_at")
    except GuardInputError:
        return []
    maximum = policy.get("max_approval_lifetime_seconds")
    if maximum is not None:
        lifetime = (end - start).total_seconds()
        return [finding("APPROVAL_LIFETIME", "approval.expires_at", lifetime,
                        {"max_seconds": maximum}, "Approval lifetime must fit reviewed policy.",
                        "PASS" if 0 < lifetime <= maximum else "FAIL")]
    return []


def _preview_checks(proposal, live_state, baseline, policy, preview, checked_at):
    required = policy.get("require_server_dry_run", False)
    if preview is None:
        return [finding("SERVER_DRY_RUN_REQUIRED" if required else "SERVER_DRY_RUN_DISABLED",
                        "preview", None, "fresh bound server result" if required else "optional",
                        "Server-side admission validation is required." if required else "Server preview is not required by policy.",
                        "UNKNOWN" if required else "SKIP")]
    try:
        mapping(preview, "preview")
        expected_fields = {"status", "proposal_sha256", "target", "resource_version",
                           "template_sha256", "checked_at", "details_code", "admitted_template_sha256"}
        if set(preview) != expected_fields or preview["status"] not in ("PASSED", "REJECTED", "UNKNOWN"):
            raise GuardInputError("preview: malformed result")
        timestamp = utc_timestamp(preview["checked_at"], "preview.checked_at")
        if not isinstance(preview["details_code"], str) or not preview["details_code"].strip():
            raise GuardInputError("preview: missing status detail")
        version = live_state["deployment"]["metadata"]["resourceVersion"]
        # A RESTART previews its bound candidate (baseline plus token); restart.py binds that hash.
        template_hash = (proposal["parameters"]["template_sha256"] if proposal["action"] == "RESTART"
                         else json_sha256(baseline["pod_template"]))
        linked = (
            preview["proposal_sha256"] == json_sha256(proposal)
            and preview["target"] == proposal["target"]
            and preview["resource_version"] == version
            and preview["template_sha256"] == template_hash
        )
    except (GuardInputError, KeyError, TypeError):
        return [finding("SERVER_DRY_RUN_INVALID", "preview", "malformed", "valid bound result",
                        "Server preview could not be validated.", "UNKNOWN")]
    if not linked:
        return [finding("SERVER_DRY_RUN_MISMATCH", "preview", "different inputs", "current exact inputs",
                        "The preview belongs to another proposal, baseline or state.")]
    age = (checked_at - timestamp).total_seconds()
    if not 0 <= age <= policy.get("max_server_dry_run_age_seconds", 10):
        return [finding("SERVER_DRY_RUN_STALE", "preview.checked_at", age, "fresh nonfuture result",
                        "Obtain a fresh server preview.", "UNKNOWN")]
    status = preview["status"]
    if status == "PASSED" and preview["admitted_template_sha256"] != template_hash:
        status = "REJECTED"
    return [finding("SERVER_DRY_RUN_" + status, "preview.status", status, "PASSED",
                    "Deployment admission preview must accept the exact template; runtime health is checked separately.",
                    {"PASSED": "PASS", "REJECTED": "FAIL", "UNKNOWN": "UNKNOWN"}[status])]


def _rollback_change_checks(proposal, live_state, baseline, policy, trace):
    """A template rollback must change the current trusted desired state.

    Replica unavailability alone does not make rewriting an identical template
    useful. Kubernetes can replace a failed Pod without a Deployment rollback.
    This compares configuration fingerprints (the restart token is ignored) only
    after the core has validated baseline identity and content, so a template
    that differs from the baseline only by a restart token is already at the
    baseline: ROLLBACK is never a disguised restart. The audit report never
    includes template contents.
    """
    if proposal["action"] != "ROLLBACK" or any(
            code == "INVALID_BASELINE" or code.startswith("BASELINE_")
            for code in trace["rejected"]):
        return []
    path = "live_state.deployment.spec.template"
    try:
        deployment = mapping(live_state.get("deployment"), "live_state.deployment")
        spec = mapping(deployment.get("spec"), "live_state.deployment.spec")
        template = mapping(spec.get("template"), path)
        pod_spec = mapping(template.get("spec"), path + ".spec")
        containers = pod_spec.get("containers")
        if not isinstance(containers, list) or not containers or any(
                not isinstance(container, dict) for container in containers):
            raise GuardInputError("Live template requires container specifications")
        current_hash = json_sha256(template)
        unchanged = config_fingerprint(template) == config_fingerprint(baseline["pod_template"])
    except (GuardInputError, TypeError, ValueError, RecursionError):
        return [finding("ROLLBACK_CHANGE_UNKNOWN", path, None,
                        "a valid current pod template",
                        "The current template is unavailable; a useful rollback cannot be established.",
                        "UNKNOWN" if policy.get("profile") == "live" else "SKIP")]
    baseline_hash = baseline["template_sha256"]
    return [finding("ROLLBACK_ALREADY_AT_BASELINE" if unchanged else "ROLLBACK_CHANGES_TEMPLATE",
                    path, {"template_sha256": current_hash},
                    {"different_from_baseline_sha256": baseline_hash},
                    "The Deployment already uses the saved baseline; rollback would make no template change."
                    if unchanged else "Rollback would restore a different, previously verified template.",
                    "FAIL" if unchanged else "PASS")]


_CORE_PATHS = {
    "TARGET_NOT_ALLOWED": "policy.targets", "ACTION_NOT_ALLOWED": "policy.allowed_actions",
    "ACTION_NOT_IMPLEMENTED": "proposal.action", "LIVE_TARGET_MISMATCH": "live_state.deployment.metadata",
    "TARGET_UID_MISMATCH": "live_state.deployment.metadata.uid",
    "STALE_RESOURCE_VERSION": "live_state.deployment.metadata.resourceVersion",
    "LIVE_STATE_FROM_FUTURE": "live_state.captured_at", "STALE_LIVE_STATE": "live_state.captured_at",
    "DEPENDENCY_RESULT_MISMATCH": "dependencies", "DEPENDENCY_BLOCKED": "dependencies.blockers",
    "DEPENDENCY_UNKNOWN": "dependencies.unknowns", "STALE_DEPENDENCY_RESULT": "dependencies.checked_at",
    "DEPENDENCY_RESULT_FROM_FUTURE": "dependencies.checked_at",
}


def _core_findings(core_result, proposal, live_state, policy, trace):
    failures = trace["rejected"]
    uncertainties = trace["escalated"]
    codes = failures + uncertainties
    result = []
    if codes:
        for code in codes:
            path = _CORE_PATHS.get(code, "approval" if "APPROVAL" in code or code == "OPERATOR_REJECTED" else "baseline")
            actual = core_result["validated_resource_version"] if code == "STALE_RESOURCE_VERSION" else "check failed"
            expected = proposal["based_on_resource_version"] if code == "STALE_RESOURCE_VERSION" else "valid current trusted input"
            result.append(finding(code, path, actual, expected,
                                  "Refresh the indicated input or correct the policy violation, then revalidate.",
                                  "FAIL" if code in failures else "UNKNOWN"))
    families = (
        ("TARGET_POLICY", ("TARGET_NOT_ALLOWED",), "policy.targets"),
        ("ACTION_POLICY", ("ACTION_NOT_ALLOWED", "ACTION_NOT_IMPLEMENTED"), "proposal.action"),
        ("LIVE_IDENTITY_VERSION", ("LIVE_TARGET_MISMATCH", "TARGET_UID_MISMATCH", "STALE_RESOURCE_VERSION"), "live_state.deployment.metadata"),
        ("LIVE_TIMESTAMP", ("LIVE_STATE_FROM_FUTURE", "STALE_LIVE_STATE"), "live_state.captured_at"),
        ("BASELINE_IDENTITY_HASH", tuple(code for code in codes if code.startswith("BASELINE_")), "baseline"),
        ("DEPENDENCY_VALIDATION", tuple(code for code in codes if code.startswith("DEPENDENCY_") or code == "STALE_DEPENDENCY_RESULT"), "dependencies"),
    )
    for code, errors, path in families:
        if not set(codes).intersection(errors):
            skipped = code == "BASELINE_IDENTITY_HASH" and proposal["action"] not in TEMPLATE_ACTIONS
            result.append(finding(code, path, "not applicable" if skipped else "validated", "matching reviewed input", "Core check completed.", "SKIP" if skipped else "PASS"))
    approval_problem = any("APPROVAL" in code or code == "OPERATOR_REJECTED" for code in codes)
    # Specific approval findings above already explain failures/uncertainty.
    # A generic UNKNOWN here would also become a public reason code and prevent
    # the Coordinator from recognizing an approval-only escalation.
    if not approval_problem:
        approval_status = "PASS" if trace["approval_checked"] else "SKIP"
        result.append(finding("APPROVAL_CHECK", "approval", "see authoritative record", "matching current approval when required",
                              "Approval cannot override a hard rejection.", approval_status))
    return result


def _deadline(start, seconds):
    try:
        return start + timedelta(seconds=seconds)
    except OverflowError as exc:
        raise GuardInputError("Decision deadline exceeds supported UTC time range") from exc


def _valid_until(policy, live_state, baseline, dependencies, approval, preview, checked_at):
    limits = [_deadline(checked_at, policy.get("decision_ttl_seconds", 30))]
    for value, field, key in ((live_state, "captured_at", "max_state_age_seconds"),
                              (dependencies, "checked_at", "max_dependency_age_seconds")):
        if value is not None and key in policy:
            try:
                limits.append(_deadline(utc_timestamp(value[field], field), policy[key]))
            except (GuardInputError, KeyError, TypeError):
                pass  # A failing check already prevents approval.
    if preview is not None:
        try:
            limits.append(_deadline(utc_timestamp(preview["checked_at"], "preview.checked_at"),
                                    policy.get("max_server_dry_run_age_seconds", 10)))
        except (GuardInputError, KeyError, TypeError):
            pass
    baseline_age = policy.get("baseline_constraints", {}).get("max_age_seconds")
    if baseline is not None and baseline_age is not None:
        try:
            limits.append(_deadline(utc_timestamp(baseline["healthy_observed_at"], "baseline.healthy_observed_at"),
                                    baseline_age))
        except (GuardInputError, KeyError, TypeError):
            pass
    if isinstance(approval, dict) and "expires_at" in approval:
        try:
            limits.append(utc_timestamp(approval["expires_at"], "approval.expires_at"))
        except GuardInputError:
            pass
    return max(checked_at, min(limits))


def evaluate_with_report(core, proposal, live_state, policy, baseline, dependencies,
                         approval=None, *, now=None, preview=None):
    checked_at = _clock(now)
    validate_policy(policy)
    trace = {}
    original = core(proposal, live_state, policy, baseline, dependencies, approval, now=checked_at, _trace=trace)
    checks = _core_findings(original, proposal, live_state, policy, trace)
    checks += _rollback_change_checks(proposal, live_state, baseline, policy, trace)
    checks += guard_restart.assess(proposal, live_state, baseline, policy, checked_at, not any(
        code == "INVALID_BASELINE" or code.startswith("BASELINE_") for code in trace["rejected"]))
    checks += guard_eligibility.assess(proposal, live_state, baseline, policy, checked_at)
    checks += guard_resources.assess(proposal, live_state, baseline, policy, checked_at)
    checks += _approval_checks(policy, approval, checked_at)
    checks += _preview_checks(proposal, live_state, baseline, policy, preview, checked_at)
    rejected = list(dict.fromkeys(item["code"] for item in checks if item["status"] == "FAIL"))
    unknown = list(dict.fromkeys(item["code"] for item in checks if item["status"] == "UNKNOWN"))
    verdict = "REJECT" if rejected else "ESCALATE" if unknown else "ALLOW"
    valid_until = _valid_until(policy, live_state, baseline, dependencies, approval, preview, checked_at)
    if verdict == "ALLOW" and policy.get("profile") == "live" and valid_until <= checked_at:
        checks.append(finding("DECISION_WINDOW_EXHAUSTED", "valid_until", valid_until.isoformat(),
                              "future validity window", "Refresh evidence before authorizing live execution.", "UNKNOWN"))
        unknown.append("DECISION_WINDOW_EXHAUSTED")
        verdict = "ESCALATE"
    decision = {
        **original, "schema_version": "2.0", "verdict": verdict,
        "reason_codes": rejected or unknown or ["VALIDATION_PASSED"],
        "proposal_sha256": json_sha256(proposal),
        "inputs_sha256": input_fingerprint(proposal, live_state, policy, baseline, dependencies, approval, preview),
        "valid_until": valid_until.isoformat().replace("+00:00", "Z"),
    }
    validate_message(decision)
    report = {
        "schema_version": "1.0", "kind": "guard_report",
        "incident_id": proposal["incident_id"], "action_id": proposal["action_id"],
        "verdict": verdict, "checked_at": decision["checked_at"], "valid_until": decision["valid_until"],
        "proposal_sha256": decision["proposal_sha256"], "inputs_sha256": decision["inputs_sha256"],
        "decision_sha256": json_sha256(decision), "checks": checks,
        "input_fingerprints": {name: json_sha256(value) for name, value in (
            ("proposal", proposal), ("live_state", live_state), ("policy", policy),
            ("baseline", baseline), ("dependencies", dependencies), ("approval", approval), ("preview", preview),
        )},
    }
    return decision, report


def assert_decision_usable(result, proposal, live_state, policy, baseline, dependencies,
                           approval=None, *, now=None, preview=None):
    """For trusted calls only: hashes do not make an untrusted decision authentic."""
    validate_message(result)
    validate_message(proposal)
    checked_at = _clock(now)
    if result["kind"] != "guard_result" or result["schema_version"] != "2.0" or result["verdict"] != "ALLOW":
        raise GuardInputError("A current bound ALLOW decision is required")
    if not utc_timestamp(result["checked_at"], "checked_at") <= checked_at < utc_timestamp(result["valid_until"], "valid_until"):
        raise GuardInputError("Decision is expired or future-dated")
    if any(result[key] != proposal[key] for key in ("incident_id", "action_id", "target")):
        raise GuardInputError("Decision does not match the proposal")
    if result["proposal_sha256"] != json_sha256(proposal) or result["inputs_sha256"] != input_fingerprint(
        proposal, live_state, policy, baseline, dependencies, approval, preview
    ):
        raise GuardInputError("Decision inputs have changed; fresh validation is required")
    if result["policy_version"] != policy["policy_version"] or result["validated_resource_version"] != live_state["deployment"]["metadata"]["resourceVersion"]:
        raise GuardInputError("Decision version binding does not match current inputs")
    issued_at = utc_timestamp(result["checked_at"], "checked_at")
    if utc_timestamp(result["valid_until"], "valid_until") > _valid_until(
        policy, live_state, baseline, dependencies, approval, preview, issued_at
    ):
        raise GuardInputError("Decision lifetime exceeds policy or evidence validity")
    return True
