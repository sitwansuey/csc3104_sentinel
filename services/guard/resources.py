"""Exact CPU/memory policy checks for rollback templates and rollout budgets.

The rollout estimate deliberately prices every current/candidate Pod at the
larger effective request, and includes observed terminating replicas. It is a
policy bound for the supplied snapshot, not a reservation or proof that nodes
can schedule the workload. Admission defaults, older ReplicaSets, unreported
terminating Pods and future scale changes require live admission/capacity checks.
"""

from fractions import Fraction
import re

from services.guard.common import GuardInputError, mapping, quantity, finding


RESOURCE_NAMES = ("cpu", "memory")
RESOURCE_FIELDS = {
    "max_container_requests", "max_container_limits", "require_requests_and_limits",
}
WORKLOAD_FIELDS = {
    "max_pod_requests", "max_rollout_requests", "max_replicas", "max_surge_replicas",
}
_CONTAINER_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?\Z")


def _resource_map(value, path):
    mapping(value, path)
    if value.keys() - set(RESOURCE_NAMES):
        raise GuardInputError(f"{path}: only cpu and memory are supported")
    return {name: quantity(raw, name, f"{path}.{name}") for name, raw in value.items()}


def _container_policy(policy):
    value = mapping(policy["resource_constraints"], "policy.resource_constraints")
    if value.keys() - RESOURCE_FIELDS:
        raise GuardInputError("policy.resource_constraints: unexpected fields")
    required = value.get("require_requests_and_limits", False)
    if type(required) is not bool:
        raise GuardInputError("policy.resource_constraints.require_requests_and_limits: expected a boolean")
    return {
        "require_requests_and_limits": required,
        **{field: _resource_map(value.get(field, {}), f"policy.resource_constraints.{field}")
           for field in ("max_container_requests", "max_container_limits")},
    }


def _workload_policy(policy):
    value = mapping(policy["workload_constraints"], "policy.workload_constraints")
    if value.keys() - WORKLOAD_FIELDS:
        raise GuardInputError("policy.workload_constraints: unexpected fields")
    result = {}
    for field in ("max_pod_requests", "max_rollout_requests"):
        if field in value:
            result[field] = _resource_map(value[field], f"policy.workload_constraints.{field}")
    for field in ("max_replicas", "max_surge_replicas"):
        if field in value:
            if type(value[field]) is not int or value[field] < 0:
                raise GuardInputError(f"policy.workload_constraints.{field}: expected a nonnegative integer")
            result[field] = value[field]
    return result


def validate_policy(policy):
    """Validate optional resource policy blocks; never modify caller inputs."""
    mapping(policy, "policy")
    if "resource_constraints" in policy:
        _container_policy(policy)
    if "workload_constraints" in policy:
        _workload_policy(policy)


def _display(value, resource):
    """All supported inputs/aggregates are whole bytes or whole milliCPU."""
    if resource == "cpu":
        return f"{int(value * 1000)}m"
    for index, prefix in reversed(list(enumerate("KMGTPE", 1))):
        divisor = 1024 ** index
        if value >= divisor and value % divisor == 0:
            return f"{int(value / divisor)}{prefix}i"
    return str(int(value))


def _containers(template, path, findings, constraints=None):
    """Parse all resource values once, retaining paths and container identities."""
    if not isinstance(template, dict) or not isinstance(template.get("spec"), dict):
        findings.append(finding("INVALID_RESOURCE_TEMPLATE", path, type(template).__name__, "PodTemplateSpec with spec", "Expected a Pod template with a spec object."))
        return None
    result = {"containers": [], "initContainers": []}
    for field in result:
        items = template["spec"].get(field, [] if field == "initContainers" else None)
        field_path = f"{path}.spec.{field}"
        if not isinstance(items, list) or (field == "containers" and not items):
            findings.append(finding("INVALID_RESOURCE_TEMPLATE", field_path, type(items).__name__, "nonempty container list" if field == "containers" else "container list", "Expected a valid container list."))
            return None
        for index, container in enumerate(items):
            item_path = f"{field_path}[{index}]"
            if not isinstance(container, dict):
                findings.append(finding("INVALID_RESOURCE_TEMPLATE", item_path, type(container).__name__, "container object", "Expected a container object."))
                return None
            raw_name = container.get("name")
            name = raw_name if isinstance(raw_name, str) and len(raw_name) <= 63 and _CONTAINER_NAME.fullmatch(raw_name) else f"#{index} (invalid name)"
            label = f"Container {name!r} ({item_path})"
            resources = container.get("resources", {})
            if not isinstance(resources, dict):
                findings.append(finding("INVALID_RESOURCE_TEMPLATE", f"{item_path}.resources", type(resources).__name__, "resource object", f"{label}: resources must be an object."))
                return None
            parsed = {"requests": {}, "limits": {}}
            for group in parsed:
                values = resources.get(group, {})
                group_path = f"{item_path}.resources.{group}"
                if not isinstance(values, dict):
                    findings.append(finding("INVALID_RESOURCE_TEMPLATE", group_path, type(values).__name__, "resource object", f"{label}: {group} must be an object."))
                    return None
                for resource in RESOURCE_NAMES:
                    resource_path = f"{group_path}.{resource}"
                    if resource not in values:
                        if constraints and constraints["require_requests_and_limits"]:
                            findings.append(finding("RESOURCE_VALUES_REQUIRED", resource_path, None, "explicit quantity", f"{label}: an explicit {resource} {group[:-1]} is required."))
                        continue
                    try:
                        parsed[group][resource] = quantity(values[resource], resource, resource_path)
                    except GuardInputError as exc:
                        findings.append(finding("INVALID_RESOURCE_QUANTITY", resource_path, f"invalid {type(values[resource]).__name__} quantity", "valid exact Kubernetes quantity", f"{label}: {exc}"))
            for resource in RESOURCE_NAMES:
                limit = parsed["limits"].get(resource)
                # Kubernetes copies a supplied limit into an omitted request.
                request = parsed["requests"].get(resource, limit)
                if request is not None and limit is not None and request > limit:
                    findings.append(finding("RESOURCE_REQUEST_EXCEEDS_LIMIT", f"{item_path}.resources.requests.{resource}", _display(request, resource), _display(limit, resource), f"{label}: {resource} request exceeds its limit."))
                if constraints:
                    for group, amount, code in (
                        ("requests", request, "RESOURCE_REQUEST_EXCEEDS_POLICY"),
                        ("limits", limit, "RESOURCE_LIMIT_EXCEEDS_POLICY"),
                    ):
                        cap = constraints[f"max_container_{group}"].get(resource)
                        if cap is None:
                            continue
                        check_path = f"{item_path}.resources.{group}.{resource}"
                        if amount is None:
                            findings.append(finding("RESOURCE_VALUES_REQUIRED", check_path, None, _display(cap, resource), f"{label}: missing {resource} {group[:-1]} cannot prove compliance with the configured ceiling."))
                        else:
                            passed = amount <= cap
                            findings.append(finding(code if not passed else "RESOURCE_WITHIN_POLICY", check_path, _display(amount, resource), _display(cap, resource), f"{label}: {resource} {group[:-1]} {_display(amount, resource)} {'is within' if passed else 'exceeds'} policy maximum {_display(cap, resource)}.", "PASS" if passed else "FAIL"))
            result[field].append({"path": item_path, "name": name, "container": container, "parsed": parsed})
    return result


def _effective_requests(template, path, records, resources, findings):
    """Sum concurrent apps/sidecars, take init-phase peaks, then add overhead."""
    spec = template["spec"]
    if "resources" in spec:
        findings.append(finding("WORKLOAD_RESOURCE_UNKNOWN", f"{path}.spec.resources", "present", "container-level resource specification", "Pod-level resource budgets are not supported by this evaluator; obtain an admission-validated effective request.", "UNKNOWN"))
        return None
    if spec.get("runtimeClassName") and "overhead" not in spec:
        findings.append(finding("WORKLOAD_RESOURCE_UNKNOWN", f"{path}.spec.overhead", None, "trusted RuntimeClass overhead", "RuntimeClass can add Pod overhead at admission; an explicit trusted overhead is needed.", "UNKNOWN"))
        return None
    overhead = spec.get("overhead", {})
    if not isinstance(overhead, dict):
        findings.append(finding("INVALID_RESOURCE_TEMPLATE", f"{path}.spec.overhead", type(overhead).__name__, "resource object", "Pod overhead must be a resource object."))
        return None
    result = {}
    for resource in sorted(resources):
        values = {}
        valid = True
        for field in records:
            for record in records[field]:
                request = record["parsed"]["requests"].get(resource, record["parsed"]["limits"].get(resource))
                if request is None:
                    findings.append(finding("WORKLOAD_RESOURCE_UNKNOWN", f"{record['path']}.resources.requests.{resource}", None, "explicit request or effective limit", f"Container {record['name']!r}: missing {resource} request and limit may be changed by namespace admission defaults.", "UNKNOWN"))
                    valid = False
                else:
                    values[record["path"]] = request
        if not valid:
            continue
        running_sidecars = Fraction(0)
        init_peak = Fraction(0)
        for record in records["initContainers"]:
            item = record["container"]
            restart_policy = item.get("restartPolicy")
            if restart_policy not in (None, "Always"):
                findings.append(finding("INVALID_RESOURCE_TEMPLATE", f"{record['path']}.restartPolicy", f"invalid {type(restart_policy).__name__} restartPolicy", "Always or omitted", "Init container restartPolicy is unsupported or malformed."))
                valid = False
                break
            phase = running_sidecars + values[record["path"]]
            init_peak = max(init_peak, phase)
            if restart_policy == "Always":
                running_sidecars = phase
        if not valid:
            continue
        app_sum = sum((values[item["path"]] for item in records["containers"]), Fraction(0))
        try:
            extra = quantity(overhead.get(resource, 0), resource, f"{path}.spec.overhead.{resource}")
        except GuardInputError as exc:
            findings.append(finding("INVALID_RESOURCE_QUANTITY", f"{path}.spec.overhead.{resource}", f"invalid {type(overhead.get(resource)).__name__} quantity", "valid exact quantity", str(exc)))
            continue
        result[resource] = max(app_sum + running_sidecars, init_peak) + extra
    return result if len(result) == len(resources) else None


def _replica_count(value, path):
    if type(value) is not int or not 0 <= value <= 2 ** 31 - 1:
        raise GuardInputError(f"{path}: expected a nonnegative signed 32-bit integer")
    return value


def _int_or_percent(value, replicas, path, *, round_up):
    if type(value) is int:
        return _replica_count(value, path)
    if not isinstance(value, str) or len(value) > 32 or re.fullmatch(r"[0-9]+%", value) is None:
        raise GuardInputError(f"{path}: expected a nonnegative integer or percentage")
    percent = int(value[:-1])
    if percent > 2 ** 31 - 1:
        raise GuardInputError(f"{path}: percentage exceeds supported magnitude")
    return (replicas * percent + (99 if round_up else 0)) // 100


def _rollout_counts(live_state, findings):
    path = "live_state.deployment"
    try:
        deployment = mapping(live_state.get("deployment"), path)
        spec = mapping(deployment.get("spec"), f"{path}.spec")
        replicas = _replica_count(spec.get("replicas"), f"{path}.spec.replicas")
        strategy = mapping(spec.get("strategy", {}), f"{path}.spec.strategy")
        mode = strategy.get("type", "RollingUpdate")
        if mode == "Recreate":
            if "rollingUpdate" in strategy:
                raise GuardInputError(f"{path}.spec.strategy.rollingUpdate: incompatible with Recreate")
            surge = 0
        elif mode == "RollingUpdate":
            rolling = mapping(strategy.get("rollingUpdate", {}), f"{path}.spec.strategy.rollingUpdate")
            surge = _int_or_percent(rolling.get("maxSurge", "25%"), replicas, f"{path}.spec.strategy.rollingUpdate.maxSurge", round_up=True)
            raw_unavailable = rolling.get("maxUnavailable", "25%")
            _int_or_percent(raw_unavailable, replicas, f"{path}.spec.strategy.rollingUpdate.maxUnavailable", round_up=False)
            if isinstance(raw_unavailable, str) and int(raw_unavailable[:-1]) > 100:
                raise GuardInputError(f"{path}.spec.strategy.rollingUpdate.maxUnavailable: percentage must not exceed 100%")
            # For a tiny deployment, a valid positive percentage may round to 0.
            raw_surge = rolling.get("maxSurge", "25%")
            surge_zero = int(raw_surge[:-1]) == 0 if isinstance(raw_surge, str) else raw_surge == 0
            unavailable_zero = int(raw_unavailable[:-1]) == 0 if isinstance(raw_unavailable, str) else raw_unavailable == 0
            if surge_zero and unavailable_zero:
                raise GuardInputError(f"{path}.spec.strategy: maxSurge and maxUnavailable cannot both be zero")
        else:
            raise GuardInputError(f"{path}.spec.strategy.type: unsupported deployment strategy")
        status = mapping(deployment.get("status", {}), f"{path}.status")
        observed = _replica_count(status.get("replicas", 0), f"{path}.status.replicas")
        terminating = _replica_count(status.get("terminatingReplicas", 0), f"{path}.status.terminatingReplicas")
        return replicas, surge, max(replicas + surge, observed) + terminating
    except GuardInputError as exc:
        findings.append(finding("WORKLOAD_RESOURCE_UNKNOWN", path, None, "trustworthy replicas and rollout strategy", str(exc), "UNKNOWN"))
        return None


def assess(proposal, live_state, baseline, policy, checked_at):
    """Return detailed PASS/FAIL/UNKNOWN/SKIP findings without external I/O."""
    validate_policy(policy)
    findings = []
    if proposal.get("action") != "ROLLBACK":
        action = proposal.get("action")
        safe_action = action if isinstance(action, str) and action in {"RESTART", "SCALE", "NO_ACTION", "ESCALATE"} else "invalid action"
        return [finding("RESOURCE_CHECKS_SKIPPED", "proposal.action", safe_action, "ROLLBACK", "Resource checks apply to rollback templates.", "SKIP")]
    constraints = _container_policy(policy) if "resource_constraints" in policy else None
    workload = _workload_policy(policy) if "workload_constraints" in policy else None
    if constraints is None and workload is None:
        return [finding("RESOURCE_CHECKS_SKIPPED", "policy", None, "resource_constraints or workload_constraints", "No resource policy block is configured.", "SKIP")]
    if not isinstance(baseline, dict) or not isinstance(baseline.get("pod_template"), dict):
        return [finding("RESOURCE_CHECKS_SKIPPED", "baseline.pod_template", None, "valid rollback baseline", "Resource checks require a valid baseline; the baseline gate must reject unavailable or invalid baselines.", "SKIP")]
    template = baseline["pod_template"]
    resource_budget = bool(workload and (workload.get("max_pod_requests") or workload.get("max_rollout_requests")))
    records = _containers(template, "baseline.pod_template", findings, constraints) if constraints is not None or resource_budget else None
    if constraints is not None and not findings:
        findings.append(finding("RESOURCE_WITHIN_POLICY", "baseline.pod_template", "valid container resource values", policy["resource_constraints"], "All configured per-container resource rules passed.", "PASS"))
    if workload is None:
        return findings
    counts = _rollout_counts(live_state, findings)
    if counts is not None:
        replicas, surge, rollout_count = counts
        for field, actual, path, code in (
            ("max_replicas", replicas, "live_state.deployment.spec.replicas", "WORKLOAD_REPLICAS_EXCEED_POLICY"),
            ("max_surge_replicas", surge, "live_state.deployment.spec.strategy", "WORKLOAD_SURGE_EXCEEDS_POLICY"),
        ):
            if field in workload:
                passed = actual <= workload[field]
                findings.append(finding("WORKLOAD_WITHIN_POLICY" if passed else code, path, actual, workload[field], f"{field}: calculated {actual}, configured maximum {workload[field]}.", "PASS" if passed else "FAIL"))
    resources = set(workload.get("max_pod_requests", {})) | set(workload.get("max_rollout_requests", {}))
    candidate = _effective_requests(template, "baseline.pod_template", records, resources, findings) if resources and records is not None else None
    if candidate is not None:
        for resource, cap in workload.get("max_pod_requests", {}).items():
            passed = candidate[resource] <= cap
            findings.append(finding("WORKLOAD_WITHIN_POLICY" if passed else "WORKLOAD_POD_REQUEST_EXCEEDS_POLICY", f"baseline.pod_template.effective_requests.{resource}", _display(candidate[resource], resource), _display(cap, resource), f"Effective Pod {resource} request includes concurrent apps, restartable init sidecars, initialization peak and overhead.", "PASS" if passed else "FAIL"))
    rollout_resources = set(workload.get("max_rollout_requests", {}))
    if rollout_resources and counts is not None and candidate is not None:
        deployment = live_state["deployment"]
        current_template = deployment["spec"].get("template")
        if not isinstance(current_template, dict):
            findings.append(finding("WORKLOAD_RESOURCE_UNKNOWN", "live_state.deployment.spec.template", type(current_template).__name__, "trusted current Pod template", "The current template is required to price old Pods during rollout.", "UNKNOWN"))
        else:
            old_records = _containers(current_template, "live_state.deployment.spec.template", findings)
            current = _effective_requests(current_template, "live_state.deployment.spec.template", old_records, rollout_resources, findings) if old_records is not None else None
            if current is not None:
                for resource, cap in workload["max_rollout_requests"].items():
                    bound = max(current[resource], candidate[resource]) * counts[2]
                    passed = bound <= cap
                    findings.append(finding("WORKLOAD_WITHIN_POLICY" if passed else "WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY", f"live_state.deployment.rollout_requests.{resource}", {"request_bound": _display(bound, resource), "priced_replicas": counts[2], "per_pod_max": _display(max(current[resource], candidate[resource]), resource)}, _display(cap, resource), "Conservative snapshot estimate: max(current, candidate) effective request times max(desired + surge, observed replicas), plus observed terminating replicas; this is not a schedulability guarantee.", "PASS" if passed else "FAIL"))
    if not findings:
        findings.append(finding("WORKLOAD_WITHIN_POLICY", "policy.workload_constraints", "no configured ceilings", policy["workload_constraints"], "No aggregate resource ceiling was configured.", "PASS"))
    return findings
