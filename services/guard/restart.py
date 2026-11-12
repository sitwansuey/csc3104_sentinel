"""Pure RESTART eligibility checks for trusted Guard snapshots; no I/O.

A RESTART writes the reviewed baseline Pod template plus a restart token, so
Kubernetes replaces every Pod. The Guard allows it only when:

- the policy allowlists the target for RESTART and bounds how often it may
  restart (restart_constraints);
- the live configuration equals the reviewed baseline, so the restart cannot
  carry a configuration change (otherwise ROLLBACK is the right action);
- the planner-bound parameters match the incident, the baseline and the exact
  resulting template, and the live template does not already carry the token;
- the trusted Deployment status shows Pods that are not ready or available;
- the target's persisted restart history (live_state.restart_history, supplied
  by the Executor ledger) leaves room in the configured window.

The security, health-evidence and resource checks in eligibility.py and
resources.py also run for RESTART, against the same baseline template.
"""

from datetime import timedelta

from services.guard.common import GuardInputError, finding, json_sha256, mapping, string, utc_timestamp
from shared.actions import RESTART_HISTORY_LIMIT, config_fingerprint, current_restart_token, restart_template, restart_token


_TARGET_FIELDS = {"cluster_id", "namespace", "kind", "name", "uid"}
_FIELDS = {"targets", "max_restarts", "window_seconds"}
MAX_RESTARTS = 100
MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
_HISTORY_LIMIT = RESTART_HISTORY_LIMIT


def validate_policy(policy):
    """Reject a malformed restart block; live use of RESTART requires one."""
    if "restart_constraints" not in policy:
        if policy.get("profile") == "live" and "RESTART" in policy.get("allowed_actions", []):
            raise GuardInputError("Live profile requires restart_constraints when RESTART is allowed")
        return
    rules = mapping(policy["restart_constraints"], "policy.restart_constraints")
    if set(rules) != _FIELDS:
        raise GuardInputError("policy.restart_constraints: expected targets, max_restarts and window_seconds")
    targets = rules["targets"]
    if not isinstance(targets, list):
        raise GuardInputError("policy.restart_constraints.targets: expected a list")
    for index, target in enumerate(targets):
        path = f"policy.restart_constraints.targets[{index}]"
        if set(mapping(target, path)) != _TARGET_FIELDS:
            raise GuardInputError(f"{path}: expected the full target identity")
        for key in _TARGET_FIELDS:
            string(target[key], f"{path}.{key}")
    for key, maximum in (("max_restarts", MAX_RESTARTS), ("window_seconds", MAX_WINDOW_SECONDS)):
        value = rules[key]
        if type(value) is not int or not 1 <= value <= maximum:
            raise GuardInputError(f"policy.restart_constraints.{key}: expected an integer from 1 to {maximum}")


def _allowlist(proposal, rules):
    allowed = rules is not None and proposal["target"] in rules["targets"]
    return finding("RESTART_TARGET_ALLOWED" if allowed else "RESTART_TARGET_NOT_ALLOWED",
                   "policy.restart_constraints.targets", proposal["target"]["name"],
                   "target allowlisted for RESTART",
                   "The policy allowlists this target for RESTART." if allowed
                   else "The policy does not allowlist this target for RESTART.",
                   "PASS" if allowed else "FAIL")


def _count(value, path):
    if value is None:
        return 0
    if type(value) is not int or value < 0:
        raise GuardInputError(f"{path}: expected a nonnegative integer")
    return value


def _justification(live_state):
    """Trusted Deployment status must show Pods that are not ready or not available."""
    path = "live_state.deployment.status"
    try:
        deployment = mapping(live_state.get("deployment"), "live_state.deployment")
        metadata = mapping(deployment.get("metadata"), "live_state.deployment.metadata")
        spec = mapping(deployment.get("spec"), "live_state.deployment.spec")
        status = mapping(deployment.get("status"), path)
        desired = _count(spec.get("replicas", 1), "live_state.deployment.spec.replicas")
        ready = _count(status.get("readyReplicas"), path + ".readyReplicas")
        available = _count(status.get("availableReplicas"), path + ".availableReplicas")
        generation = _count(metadata.get("generation"), "live_state.deployment.metadata.generation")
        observed = _count(status.get("observedGeneration"), path + ".observedGeneration")
    except GuardInputError:
        return finding("RESTART_JUSTIFICATION_UNKNOWN", path, "unavailable", "trusted Deployment status",
                       "Pod failure cannot be established from the current Deployment status.", "UNKNOWN")
    actual = {"desired": desired, "ready": ready, "available": available}
    if observed < generation:
        return finding("RESTART_JUSTIFICATION_UNKNOWN", path + ".observedGeneration", observed, generation,
                       "The Deployment controller has not observed the latest generation; a rollout may be in progress.",
                       "UNKNOWN")
    failing = desired > 0 and min(ready, available) < desired
    return finding("RESTART_JUSTIFIED" if failing else "RESTART_NOT_JUSTIFIED", path, actual,
                   "fewer ready or available replicas than desired",
                   "Pods of the target are not ready or not available." if failing
                   else "Every desired Pod is ready and available, so there is no Pod failure to restart.",
                   "PASS" if failing else "FAIL")


def _budget(live_state, rules, checked_at):
    path = "live_state.restart_history"
    if rules is None:
        return finding("RESTART_BUDGET_UNKNOWN", "policy.restart_constraints", None, "configured restart budget",
                       "No restart budget is configured for this target.", "UNKNOWN")
    history = live_state.get("restart_history")
    try:
        if not isinstance(history, list) or len(history) > _HISTORY_LIMIT:
            raise GuardInputError(f"{path}: expected a bounded list")
        stamps = []
        for index, entry in enumerate(history):
            entry = mapping(entry, f"{path}[{index}]")
            if set(entry) != {"action_id", "started_at"}:
                raise GuardInputError(f"{path}[{index}]: expected action_id and started_at")
            string(entry["action_id"], f"{path}[{index}].action_id")
            stamps.append(utc_timestamp(entry["started_at"], f"{path}[{index}].started_at"))
    except GuardInputError:
        return finding("RESTART_BUDGET_UNKNOWN", path, "unavailable", "persisted restart history",
                       "The target's restart history is unavailable, so the restart budget cannot be checked.",
                       "UNKNOWN")
    since = checked_at - timedelta(seconds=rules["window_seconds"])
    # A future-dated entry still counts: a skewed clock must not reopen the budget.
    used = sum(1 for stamp in stamps if stamp > since)
    exhausted = used >= rules["max_restarts"]
    return finding("RESTART_BUDGET_EXHAUSTED" if exhausted else "RESTART_BUDGET_AVAILABLE", path,
                   {"restarts_in_window": used},
                   {"max_restarts": rules["max_restarts"], "window_seconds": rules["window_seconds"]},
                   "The target has used its restart budget for this window; ESCALATE instead." if exhausted
                   else "The target has restart budget left in this window.",
                   "FAIL" if exhausted else "PASS")


def _template_checks(proposal, live_state, baseline):
    output = []
    path = "live_state.deployment.spec.template"
    try:
        deployment = mapping(live_state.get("deployment"), "live_state.deployment")
        live = mapping(mapping(deployment.get("spec"), "live_state.deployment.spec").get("template"), path)
        unchanged = config_fingerprint(live) == config_fingerprint(baseline["pod_template"])
    except (GuardInputError, TypeError, ValueError, RecursionError):
        return [finding("RESTART_CONFIG_UNKNOWN", path, None, "a valid current Pod template",
                        "The current template is unavailable; a configuration-preserving restart cannot be established.",
                        "UNKNOWN")]
    output.append(finding("RESTART_CONFIG_UNCHANGED" if unchanged else "RESTART_TEMPLATE_DRIFT", path,
                          {"template_sha256": json_sha256(live)},
                          {"same_configuration_as_baseline_sha256": baseline["template_sha256"]},
                          "The live configuration equals the reviewed baseline; only the restart token changes."
                          if unchanged else "The live configuration differs from the reviewed baseline. A restart "
                          "would keep that change; use ROLLBACK to restore the baseline.",
                          "PASS" if unchanged else "FAIL"))
    parameters = proposal["parameters"]
    expected = restart_token(proposal["incident_id"], baseline["baseline_id"])
    bound = (parameters["restart_token"] == expected
             and parameters["template_sha256"] == json_sha256(restart_template(baseline["pod_template"], expected)))
    output.append(finding("RESTART_BINDING" if bound else "RESTART_BINDING_MISMATCH", "proposal.parameters",
                          {"restart_token": parameters["restart_token"], "template_sha256": parameters["template_sha256"]},
                          "token and template bound to this incident and baseline",
                          "The restart token and resulting template are bound to this incident and baseline."
                          if bound else "The restart parameters are not the planner-bound values for this incident and baseline.",
                          "PASS" if bound else "FAIL"))
    reused = current_restart_token(live) == parameters["restart_token"]
    output.append(finding("RESTART_TOKEN_REUSED" if reused else "RESTART_TOKEN_FRESH",
                          path + ".metadata.annotations", current_restart_token(live), "a different restart token",
                          "The live template already carries this restart token; this incident has already restarted."
                          if reused else "The live template does not carry this restart token.",
                          "FAIL" if reused else "PASS"))
    return output


def assess(proposal, live_state, baseline, policy, checked_at, baseline_valid):
    """Return RESTART findings; empty for every other action."""
    if proposal.get("action") != "RESTART":
        return []
    rules = policy.get("restart_constraints")
    output = [_allowlist(proposal, rules), _justification(live_state), _budget(live_state, rules, checked_at)]
    if baseline_valid:
        output.extend(_template_checks(proposal, live_state, baseline))
    return output
