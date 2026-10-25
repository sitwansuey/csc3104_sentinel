"""Owner 6: dependency impact before execution; sustained health after execution."""

from copy import deepcopy
from datetime import datetime, timedelta

from shared.contracts import ContractError, validate_message


class VerifierInputError(ContractError):
    """The caller supplied an invalid message or verification policy."""


def _timestamp(value):
    if not isinstance(value, str):
        raise VerifierInputError("Expected a UTC timestamp")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise VerifierInputError("Expected a UTC timestamp") from exc
    if stamp.utcoffset() != timedelta(0):
        raise VerifierInputError("Expected a timezone-aware UTC timestamp")
    return stamp


def _format_time(value):
    return value.isoformat().replace("+00:00", "Z")


def _envelope(message, kind):
    return {
        "schema_version": "1.0", "kind": kind,
        "incident_id": message["incident_id"],
        "action_id": message["action_id"],
        "target": deepcopy(message["target"]),
    }


def _check(name, passed, detail):
    return {"name": name, "passed": bool(passed), "detail": detail}


def _health_policy(policy):
    if not isinstance(policy, dict) or not isinstance(policy.get("health"), dict):
        raise VerifierInputError("policy.health must be an object")
    rules = policy["health"]
    for key in ("minimum_samples", "stable_window_seconds", "max_sample_gap_seconds"):
        if type(rules.get(key)) is not int or rules[key] < 1:
            raise VerifierInputError(f"policy.health.{key} must be a positive integer")
    status = rules.get("required_http_status")
    if type(status) is not int or not 100 <= status <= 599:
        raise VerifierInputError("policy.health.required_http_status must be an HTTP status")
    required = rules.get("required_dependencies", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or not name.strip() for name in required
    ):
        raise VerifierInputError("policy.health.required_dependencies must be a list of names")
    return rules


def inspect_dependencies(proposal: dict, graph: dict, live_state: dict) -> dict:
    """Traverse caller-to-callee edges; handle cycles, unhealthy and unknown nodes."""
    validate_message(proposal)
    raise NotImplementedError("Implement CLEAR/BLOCKED/UNKNOWN dependency checks")


def verify(execution: dict, samples: list[dict], policy: dict) -> dict:
    """Assess one ordered post-action window; missing evidence is UNKNOWN.

    This offline function checks supplied evidence, not current cluster health.
    A caller may retry with a new complete window after an unhealthy result.
    """
    validate_message(execution)
    if execution["kind"] != "execution_result":
        raise VerifierInputError("Expected an execution_result")
    rules = _health_policy(policy)
    if not isinstance(samples, list):
        raise VerifierInputError("samples must be a list")
    executed_at = _timestamp(execution["recorded_at"])
    checks = []
    usable_execution = execution["status"] in {"APPLIED", "NOOP"} and bool(execution["resource_version"])
    checks.append(_check("execution", usable_execution, "Requires an applied/no-op action with resource-version evidence"))
    checks.append(_check("sample_count", len(samples) >= rules["minimum_samples"],
                         f"Received {len(samples)} readings; at least {rules['minimum_samples']} required"))

    stamps, rows = [], []
    identity_ok = timestamps_ok = health_complete = dependencies_complete = True
    unhealthy_service = unhealthy_dependency = False
    required_dependencies = set(rules.get("required_dependencies", []))
    for sample in samples:
        if not isinstance(sample, dict):
            identity_ok = timestamps_ok = health_complete = dependencies_complete = False
            continue
        matching = sample.get("service") == execution["target"]["name"]
        if "target" in sample:
            matching = matching and sample["target"] == execution["target"]
        identity_ok = identity_ok and matching
        try:
            at = _timestamp(sample.get("at"))
        except VerifierInputError:
            timestamps_ok = False
            continue
        if at < executed_at or (stamps and at <= stamps[-1]):
            timestamps_ok = False
        stamps.append(at)
        rows.append(sample)
        http, ready, desired = (sample.get(key) for key in ("http_status", "ready_replicas", "desired_replicas"))
        complete = (type(http) is int and 100 <= http <= 599
                    and type(ready) is int and ready >= 0
                    and type(desired) is int and desired > 0)
        health_complete = health_complete and complete
        if complete:
            unhealthy_service |= http != rules["required_http_status"] or ready < desired
        dependencies = sample.get("dependencies")
        if not isinstance(dependencies, dict):
            dependencies_complete = False
        else:
            for name, healthy in dependencies.items():
                if not isinstance(name, str) or not name.strip():
                    dependencies_complete = False
                else:
                    required_dependencies.add(name)
                if type(healthy) is not bool:
                    dependencies_complete = False
                elif not healthy:
                    unhealthy_dependency = True

    # Once a dependency is declared or observed, every reading must cover it.
    for sample in rows:
        dependencies = sample.get("dependencies")
        if not isinstance(dependencies, dict) or not required_dependencies <= dependencies.keys():
            dependencies_complete = False
    checks.append(_check("sample_identity", identity_ok, "All readings must belong to the execution target"))
    checks.append(_check("sample_timestamps", timestamps_ok,
                         "Readings must be UTC, strictly ordered and at/after execution"))
    valid_times = bool(stamps) and timestamps_ok
    duration = (stamps[-1] - stamps[0]).total_seconds() if valid_times else 0
    gaps_ok = valid_times and all(
        (right - left).total_seconds() <= rules["max_sample_gap_seconds"]
        for left, right in zip(stamps, stamps[1:])
    )
    checks.append(_check("stable_window", valid_times and duration >= rules["stable_window_seconds"],
                         f"Observed {duration:g} seconds; at least {rules['stable_window_seconds']} required"))
    checks.append(_check("sample_gaps", gaps_ok, f"No gap may exceed {rules['max_sample_gap_seconds']} seconds"))
    checks.append(_check("service_health", bool(rows) and health_complete and not unhealthy_service,
                         "Requires complete successful HTTP and positive desired-replica readiness readings"))
    checks.append(_check("dependency_health", bool(rows) and dependencies_complete and not unhealthy_dependency,
                         "Every required/observed dependency must be present and healthy in every reading"))

    trustworthy = (usable_execution and identity_ok and timestamps_ok
                   and health_complete and dependencies_complete)
    status = "UNKNOWN"
    if trustworthy and (unhealthy_service or unhealthy_dependency):
        status = "NOT_RECOVERED"
    elif all(check["passed"] for check in checks):
        status = "RECOVERED"
    result = _envelope(execution, "outcome")
    result.update(status=status, checks=checks,
                  window_start=_format_time(stamps[0]) if valid_times else _format_time(executed_at),
                  window_end=_format_time(stamps[-1]) if valid_times else _format_time(executed_at))
    validate_message(result)
    return result
