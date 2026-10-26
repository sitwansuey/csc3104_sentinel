"""Owner 6: dependency impact before execution; sustained health after execution."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re

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
    rules = deepcopy(policy["health"])
    protocol = rules.setdefault("protocol", "http")
    if protocol not in {"http", "grpc"}:
        raise VerifierInputError("policy.health.protocol must be http or grpc")
    for key in ("minimum_samples", "stable_window_seconds", "max_sample_gap_seconds"):
        if type(rules.get(key)) is not int or rules[key] < 1:
            raise VerifierInputError(f"policy.health.{key} must be a positive integer")
    if protocol == "http":
        status = rules.get("required_http_status")
        if type(status) is not int or not 100 <= status <= 599:
            raise VerifierInputError("policy.health.required_http_status must be an HTTP status")
    else:
        status = rules.get("required_grpc_status")
        if type(status) is not int or status != 0:
            raise VerifierInputError("policy.health.required_grpc_status must be 0 (OK)")
    for key in ("require_target_identity", "require_template_match", "require_available_replicas"):
        if key in rules and type(rules[key]) is not bool:
            raise VerifierInputError(f"policy.health.{key} must be a boolean")
    if rules.get("require_template_match") and not re.fullmatch(
            r"[a-f0-9]{64}", str(rules.get("expected_template_sha256", ""))):
        raise VerifierInputError("A verified expected template SHA-256 is required")
    required = rules.get("required_dependencies", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or not name.strip() for name in required
    ):
        raise VerifierInputError("policy.health.required_dependencies must be a list of names")
    return rules


def _reachable(start, adjacency):
    visited, pending = set(), [start]
    while pending:
        node = pending.pop()
        if node not in visited:
            visited.add(node)
            pending.extend(adjacency.get(node, set()) - visited)
    return visited


def inspect_dependencies(proposal: dict, graph: dict, live_state: dict,
                         *, now: datetime | None = None) -> dict:
    """Assess the target's callers and callees from trusted offline evidence.

    The failing target is intentionally not its own dependency blocker. UNKNOWN
    means incomplete evidence, not permission. Guard makes the final decision.
    """
    validate_message(proposal)
    if proposal["kind"] != "proposal":
        raise VerifierInputError("Expected a proposal")
    if not isinstance(graph, dict) or graph.get("edge_direction") != "caller_to_callee":
        raise VerifierInputError("graph must declare caller_to_callee edges")
    nodes, edges = graph.get("nodes"), graph.get("edges")
    if not isinstance(nodes, list) or any(
        not isinstance(node, str) or not node.strip() for node in nodes
    ):
        raise VerifierInputError("graph.nodes must be a list of service names")
    if not isinstance(edges, list) or not isinstance(live_state, dict):
        raise VerifierInputError("Expected graph.edges list and live_state object")
    checked_at = datetime.now(timezone.utc) if now is None else now
    if not isinstance(checked_at, datetime) or checked_at.utcoffset() != timedelta(0):
        raise VerifierInputError("now must be a timezone-aware UTC datetime")
    names = set(nodes)
    forward, reverse = {node: set() for node in names}, {node: set() for node in names}
    unknowns = set()
    for edge in edges:
        if not isinstance(edge, dict) or any(
            not isinstance(edge.get(key), str) or not edge[key].strip() for key in ("from", "to")
        ):
            raise VerifierInputError("Each edge must name its caller and callee")
        caller, callee = edge["from"], edge["to"]
        forward.setdefault(caller, set()).add(callee)
        reverse.setdefault(callee, set()).add(caller)
        unknowns.update(f"undeclared-node: {node}" for node in (caller, callee) if node not in names)

    root = proposal["target"]["name"]
    affected = _reachable(root, forward) | _reachable(root, reverse)
    if root not in names:
        unknowns.add(f"undeclared-node: {root}")
    if proposal["action"] not in {"ROLLBACK", "RESTART", "SCALE"}:
        unknowns.add("proposal-does-not-request-recovery")
    if "target" in live_state and live_state["target"] != proposal["target"]:
        unknowns.add("live-target-mismatch")
    if "deployment" in live_state:
        deployment = live_state["deployment"]
        metadata = deployment.get("metadata", {}) if isinstance(deployment, dict) else {}
        expected = proposal["target"]
        if (not isinstance(metadata, dict) or not isinstance(deployment, dict)
                or deployment.get("kind") != expected["kind"]
                or live_state.get("cluster_id") != expected["cluster_id"]
                or any(metadata.get(key) != expected[key] for key in ("name", "namespace", "uid"))):
            unknowns.add("live-target-mismatch")
        if isinstance(metadata, dict) and metadata.get("resourceVersion") != proposal["based_on_resource_version"]:
            unknowns.add("live-resource-version-mismatch")
    if "captured_at" in live_state:
        try:
            if _timestamp(live_state["captured_at"]) > checked_at:
                unknowns.add("live-state-from-future")
        except VerifierInputError:
            unknowns.add("invalid-live-state-time")

    health = graph.get("known_health", {})
    if not isinstance(health, dict):
        raise VerifierInputError("graph.known_health must be an object")
    health = dict(health)
    if "known_health" in live_state:
        if not isinstance(live_state["known_health"], dict):
            raise VerifierInputError("live_state.known_health must be an object")
        health.update(live_state["known_health"])
    blockers = []
    for service in sorted(affected - {root}):
        if type(health.get(service)) is not bool:
            unknowns.add(service)
        elif not health[service]:
            blockers.append(service)
    result = _envelope(proposal, "dependency_result")
    result.update(status="BLOCKED" if blockers else "UNKNOWN" if unknowns else "CLEAR",
                  affected_services=sorted(affected), blockers=blockers,
                  unknowns=sorted(unknowns), checked_at=_format_time(checked_at))
    validate_message(result)
    return result


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
    configuration_complete = True
    unhealthy_service = unhealthy_dependency = unhealthy_template = False
    required_dependencies = set(rules.get("required_dependencies", []))
    for sample in samples:
        if not isinstance(sample, dict):
            identity_ok = timestamps_ok = health_complete = dependencies_complete = False
            continue
        matching = sample.get("service") == execution["target"]["name"]
        if "target" in sample or rules.get("require_target_identity"):
            matching = matching and sample.get("target") == execution["target"]
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
        ready, desired = (sample.get(key) for key in ("ready_replicas", "desired_replicas"))
        protocol_matches = sample.get("protocol", "http") == rules["protocol"]
        if rules["protocol"] == "grpc":
            status, application_ok = sample.get("grpc_status"), sample.get("application_check_passed")
            response_complete = type(status) is int and 0 <= status <= 16 and type(application_ok) is bool
            response_healthy = status == rules["required_grpc_status"] and application_ok is True
        else:
            status = sample.get("http_status")
            response_complete = type(status) is int and 100 <= status <= 599
            response_healthy = status == rules["required_http_status"]
        complete = (protocol_matches and response_complete and type(ready) is int and ready >= 0
                    and type(desired) is int and desired > 0)
        available = sample.get("available_replicas")
        if rules.get("require_available_replicas"):
            complete = complete and type(available) is int and available >= 0
        health_complete = health_complete and complete
        if complete:
            unhealthy_service |= not response_healthy or ready < desired
            if rules.get("require_available_replicas"):
                unhealthy_service |= available < desired
        if rules.get("require_template_match"):
            template = sample.get("template_sha256")
            valid_hash = isinstance(template, str) and re.fullmatch(r"[a-f0-9]{64}", template) is not None
            configuration_complete = (configuration_complete and valid_hash
                                      and sample.get("configuration_stable") is True)
            if valid_hash:
                unhealthy_template |= template != rules["expected_template_sha256"]
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
                         "Requires complete successful " + rules["protocol"].upper()
                         + " application and positive desired-replica readiness readings"))
    checks.append(_check("dependency_health", bool(rows) and dependencies_complete and not unhealthy_dependency,
                         "Every required/observed dependency must be present and healthy in every reading"))
    if rules.get("require_template_match"):
        checks.append(_check("sample_configuration", bool(rows) and configuration_complete and not unhealthy_template,
                             "Each reading must retain the approved template and a stable observation identity"))

    trustworthy = (usable_execution and identity_ok and timestamps_ok
                   and health_complete and dependencies_complete and configuration_complete)
    status = "UNKNOWN"
    if trustworthy and (unhealthy_service or unhealthy_dependency or unhealthy_template):
        status = "NOT_RECOVERED"
    elif all(check["passed"] for check in checks):
        status = "RECOVERED"
    result = _envelope(execution, "outcome")
    result.update(status=status, checks=checks,
                  window_start=_format_time(stamps[0]) if valid_times else _format_time(executed_at),
                  window_end=_format_time(stamps[-1]) if valid_times else _format_time(executed_at))
    validate_message(result)
    return result
