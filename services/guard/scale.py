"""Pure SCALE necessity checks for trusted Guard snapshots; no I/O (P5.07).

A SCALE restores the replica count recorded in a reviewed baseline after
something scaled the workload below it. It changes spec.replicas only. The
Guard allows it only when:

- the policy allowlists the target for SCALE (scale_constraints.targets);
- the requested count is the baseline's own count (SCALE_NOT_BASELINE), the
  baseline records one (SCALE_BASELINE_REPLICAS_UNKNOWN) and the policy's
  workload_constraints.max_replicas is configured (SCALE_REPLICA_LIMIT_UNKNOWN);
  resources.py prices the request at that count and rejects one above the
  ceiling (WORKLOAD_REPLICAS_EXCEED_POLICY);
- the live count is below it: equal is SCALE_NO_CHANGE and above is
  SCALE_WOULD_REDUCE, because Sentinel never removes capacity;
- the live configuration equals the reviewed baseline (SCALE_TEMPLATE_DRIFT:
  new Pods would run a template nobody reviewed; roll back first) and the live
  template is exactly the one the Planner bound (SCALE_BINDING_MISMATCH);
- the controller has observed the latest generation (SCALE_STATE_UNKNOWN);
- no HorizontalPodAutoscaler targets the Deployment. An autoscaler would fight
  the write, so its presence, or an unavailable autoscaler read, is UNKNOWN and
  the action escalates (SCALE_AUTOSCALER_PRESENT, SCALE_AUTOSCALER_UNKNOWN).
  live_state.autoscalers is supplied by the Executor's read-only HPA list.

The health, security and reference checks in eligibility.py also run for
SCALE: new Pods start from the live template, whose configuration is the
baseline's.
"""

from services.guard.common import GuardInputError, finding, json_sha256, mapping, string
from shared.actions import baseline_replicas, config_fingerprint

_TARGET_FIELDS = {"cluster_id", "namespace", "kind", "name", "uid"}
_AUTOSCALER_LIMIT = 100


def autoscalers_for(listing, name):
    """[{"name": ...}] for each HorizontalPodAutoscaler in a kubectl list that scales Deployment name.

    Pure; the Executor adapter and the lab harness both read the list with
    kubectl get horizontalpodautoscalers.autoscaling -o json. ValueError for a
    list that cannot be trusted, so the caller refuses or escalates.
    """
    try:
        items = listing["items"]
        if not isinstance(items, list) or len(items) > _AUTOSCALER_LIMIT:
            raise ValueError("Unbounded autoscaler list")
        names = []
        for item in items:
            reference = item["spec"]["scaleTargetRef"]
            # Any API group counts (an old extensions/v1beta1 reference still scales the Deployment): an
            # autoscaler wrongly counted only escalates, one wrongly missed would fight the write.
            if reference.get("kind") == "Deployment" and reference.get("name") == name:
                value = item["metadata"]["name"]
                if not isinstance(value, str) or not value.strip() or len(value) > 253:
                    raise ValueError("Invalid autoscaler name")
                names.append(value)
    except (AttributeError, KeyError, TypeError) as error:
        raise ValueError("Invalid autoscaler list") from error
    return [{"name": value} for value in sorted(names)]


def validate_policy(policy):
    """Reject a malformed scale block; live use of SCALE requires one."""
    if "scale_constraints" not in policy:
        if policy.get("profile") == "live" and "SCALE" in policy.get("allowed_actions", []):
            raise GuardInputError("Live profile requires scale_constraints when SCALE is allowed")
        return
    rules = mapping(policy["scale_constraints"], "policy.scale_constraints")
    if set(rules) != {"targets"}:
        raise GuardInputError("policy.scale_constraints: expected targets only")
    targets = rules["targets"]
    if not isinstance(targets, list):
        raise GuardInputError("policy.scale_constraints.targets: expected a list")
    for index, target in enumerate(targets):
        path = f"policy.scale_constraints.targets[{index}]"
        if set(mapping(target, path)) != _TARGET_FIELDS:
            raise GuardInputError(f"{path}: expected the full target identity")
        for key in _TARGET_FIELDS:
            string(target[key], f"{path}.{key}")


def _allowlist(proposal, policy):
    rules = policy.get("scale_constraints")
    allowed = rules is not None and proposal["target"] in rules["targets"]
    return finding("SCALE_TARGET_ALLOWED" if allowed else "SCALE_TARGET_NOT_ALLOWED",
                   "policy.scale_constraints.targets", proposal["target"]["name"], "target allowlisted for SCALE",
                   "The policy allowlists this target for SCALE." if allowed
                   else "The policy does not allowlist this target for SCALE.",
                   "PASS" if allowed else "FAIL")


def _count(value, path):
    if type(value) is not int or value < 0:
        raise GuardInputError(f"{path}: expected a nonnegative integer")
    return value


def _live(live_state):
    """(spec.replicas, generation, observedGeneration, template) of the trusted live read."""
    deployment = mapping(live_state.get("deployment"), "live_state.deployment")
    metadata = mapping(deployment.get("metadata"), "live_state.deployment.metadata")
    spec = mapping(deployment.get("spec"), "live_state.deployment.spec")
    status = mapping(deployment.get("status", {}), "live_state.deployment.status")
    # Kubernetes defaults an omitted spec.replicas to 1; a trusted read always carries it.
    current = _count(spec.get("replicas"), "live_state.deployment.spec.replicas")
    generation = _count(metadata.get("generation"), "live_state.deployment.metadata.generation")
    observed = _count(status.get("observedGeneration", 0), "live_state.deployment.status.observedGeneration")
    template = mapping(spec.get("template"), "live_state.deployment.spec.template")
    return current, generation, observed, template


def _count_checks(proposal, baseline, policy, current):
    desired = proposal["parameters"]["desired_replicas"]
    reviewed = baseline_replicas(baseline)
    output = []
    if reviewed is None:
        output.append(finding("SCALE_BASELINE_REPLICAS_UNKNOWN", "baseline.replicas", None,
                              "a reviewed replica count from 1 to 10",
                              "The baseline records no replica count, so there is no reviewed count to restore.",
                              "UNKNOWN"))
    else:
        matches = desired == reviewed
        output.append(finding("SCALE_RESTORES_BASELINE" if matches else "SCALE_NOT_BASELINE",
                              "proposal.parameters.desired_replicas", desired, {"baseline_replicas": reviewed},
                              "The requested count is the reviewed baseline's count." if matches
                              else "SCALE may only restore the reviewed baseline's replica count.",
                              "PASS" if matches else "FAIL"))
    if "max_replicas" not in policy.get("workload_constraints", {}):
        output.append(finding("SCALE_REPLICA_LIMIT_UNKNOWN", "policy.workload_constraints.max_replicas", None,
                              "a configured replica ceiling",
                              "No replica ceiling is configured, so the requested count cannot be bounded.",
                              "UNKNOWN"))
    if desired == current:
        output.append(finding("SCALE_NO_CHANGE", "live_state.deployment.spec.replicas", current, desired,
                              "The Deployment already has the requested replica count; a scale would change nothing."))
    elif desired < current:
        output.append(finding("SCALE_WOULD_REDUCE", "live_state.deployment.spec.replicas", current, desired,
                              "SCALE only restores a count that fell below the reviewed one; it never removes replicas."))
    else:
        output.append(finding("SCALE_RAISES_REPLICAS", "live_state.deployment.spec.replicas", current, desired,
                              "The live count is below the requested reviewed count.", "PASS"))
    return output


def _template_checks(proposal, baseline, template):
    try:
        unchanged = config_fingerprint(template) == config_fingerprint(baseline["pod_template"])
        digest = json_sha256(template)
    except (GuardInputError, TypeError, ValueError, RecursionError):
        return [finding("SCALE_STATE_UNKNOWN", "live_state.deployment.spec.template", None,
                        "a valid current Pod template",
                        "The current template is unavailable; a configuration-preserving scale cannot be established.",
                        "UNKNOWN")]
    bound = digest == proposal["parameters"]["template_sha256"]
    return [
        finding("SCALE_CONFIG_UNCHANGED" if unchanged else "SCALE_TEMPLATE_DRIFT",
                "live_state.deployment.spec.template", {"template_sha256": digest},
                {"same_configuration_as_baseline_sha256": baseline["template_sha256"]},
                "The live configuration equals the reviewed baseline, so new Pods run the reviewed template."
                if unchanged else "The live configuration differs from the reviewed baseline. Scaling up would start "
                "more Pods from an unreviewed template; restore the baseline with ROLLBACK first.",
                "PASS" if unchanged else "FAIL"),
        finding("SCALE_BINDING" if bound else "SCALE_BINDING_MISMATCH", "proposal.parameters.template_sha256",
                proposal["parameters"]["template_sha256"], {"live_template_sha256": digest},
                "The proposal names the exact live template the scale leaves in place." if bound
                else "The proposal names another Pod template than the live one.",
                "PASS" if bound else "FAIL"),
    ]


def _autoscalers(live_state):
    path = "live_state.autoscalers"
    value = live_state.get("autoscalers")
    try:
        if not isinstance(value, list) or len(value) > _AUTOSCALER_LIMIT:
            raise GuardInputError(path + ": expected a bounded list")
        names = []
        for index, item in enumerate(value):
            entry = mapping(item, f"{path}[{index}]")
            if set(entry) != {"name"}:
                raise GuardInputError(f"{path}[{index}]: expected name only")
            names.append(string(entry["name"], f"{path}[{index}].name"))
    except GuardInputError:
        return finding("SCALE_AUTOSCALER_UNKNOWN", path, "unavailable",
                       "a trusted list of HorizontalPodAutoscalers targeting the Deployment",
                       "Whether an autoscaler manages this Deployment is unknown; an autoscaler would fight the scale.",
                       "UNKNOWN")
    if names:
        return finding("SCALE_AUTOSCALER_PRESENT", path, sorted(names)[:10], [],
                       "A HorizontalPodAutoscaler targets this Deployment and would fight a manual scale; "
                       "an operator must review it.", "UNKNOWN")
    return finding("SCALE_NO_AUTOSCALER", path, [], [], "No HorizontalPodAutoscaler targets this Deployment.", "PASS")


def assess(proposal, live_state, baseline, policy, baseline_valid):
    """Return SCALE findings; empty for every other action."""
    if proposal.get("action") != "SCALE":
        return []
    output = [_allowlist(proposal, policy), _autoscalers(live_state)]
    try:
        current, generation, observed, template = _live(live_state)
    except GuardInputError:
        output.append(finding("SCALE_STATE_UNKNOWN", "live_state.deployment", "unavailable",
                              "trusted spec.replicas, generation and Pod template",
                              "The current replica count or template is unavailable.", "UNKNOWN"))
        return output
    if observed < generation:
        output.append(finding("SCALE_STATE_UNKNOWN", "live_state.deployment.status.observedGeneration",
                              observed, generation,
                              "The Deployment controller has not observed the latest generation; a change may be "
                              "in progress.", "UNKNOWN"))
    if not baseline_valid:
        return output  # The core baseline checks already reject; nothing reviewed to compare against.
    output.extend(_count_checks(proposal, baseline, policy, current))
    output.extend(_template_checks(proposal, baseline, template))
    return output
