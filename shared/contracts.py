"""Closed JSON wire contracts for the first Sentinel team integration.

This module validates message shape, not policy safety or recovery success.
It deliberately accepts no arbitrary executable command in an action proposal.
"""

from datetime import datetime, timedelta
import re


class ContractError(ValueError):
    pass


ACTION_TYPES = frozenset({"RESTART", "SCALE", "ROLLBACK", "NO_ACTION", "ESCALATE"})


def _object(value, required, optional=(), path="message"):
    if not isinstance(value, dict):
        raise ContractError(f"{path}: expected an object")
    missing = set(required) - value.keys()
    extra = value.keys() - set(required) - set(optional)
    if missing or extra:
        raise ContractError(f"{path}: missing={sorted(missing)}, unexpected={sorted(extra)}")


def _string(value, path, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise ContractError(f"{path}: expected a {'possibly empty ' if allow_empty else 'nonempty '}string")


def _integer(value, path, minimum=0):
    if type(value) is not int or value < minimum:
        raise ContractError(f"{path}: expected an integer >= {minimum}")


def _timestamp(value, path):
    _string(value, path)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{path}: invalid ISO timestamp") from exc
    if result.utcoffset() != timedelta(0):
        raise ContractError(f"{path}: expected a timezone-aware UTC timestamp")
    return result


def _strings(value, path, minimum=0):
    if not isinstance(value, list) or len(value) < minimum:
        raise ContractError(f"{path}: expected a list with at least {minimum} items")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]")


def _enum(value, choices, path):
    if not isinstance(value, str) or value not in choices:
        raise ContractError(f"{path}: expected one of {sorted(choices)}")


def _target(target):
    _object(target, {"cluster_id", "namespace", "kind", "name", "uid"}, path="target")
    for key, value in target.items():
        _string(value, f"target.{key}")
    _enum(target["kind"], {"Deployment"}, "target.kind")


FIELDS = {
    "incident": {"incident_id", "observed_at", "observed_resource_version", "observed_generation", "signals"},
    "proposal": {"incident_id", "action_id", "action", "parameters", "based_on_resource_version", "rationale", "evidence_refs"},
    "guard_result": {"incident_id", "action_id", "verdict", "reason_codes", "policy_version", "validated_resource_version", "checked_at"},
    "dependency_result": {"incident_id", "action_id", "status", "affected_services", "blockers", "unknowns", "checked_at"},
    "execution_result": {"incident_id", "action_id", "status", "resource_version", "recorded_at", "details"},
    "outcome": {"incident_id", "action_id", "status", "window_start", "window_end", "checks"},
}


def validate_message(message):
    """Raise ContractError for incompatible data; never grant authorisation."""
    if not isinstance(message, dict):
        raise ContractError("message: expected an object")
    kind = message.get("kind")
    if not isinstance(kind, str) or kind not in FIELDS:
        raise ContractError("message.kind: unknown message type")
    version = message.get("schema_version")
    bound_fields = {"proposal_sha256", "inputs_sha256", "valid_until"} if kind == "guard_result" and version == "2.0" else set()
    _object(message, {"kind", "schema_version", "target"} | FIELDS[kind] | bound_fields)
    if version != "1.0" and not (kind == "guard_result" and version == "2.0"):
        raise ContractError("schema_version: unsupported message version")
    _target(message["target"])
    _string(message["incident_id"], "incident_id")
    if "action_id" in message:
        _string(message["action_id"], "action_id")

    if kind == "incident":
        _timestamp(message["observed_at"], "observed_at")
        _string(message["observed_resource_version"], "observed_resource_version")
        _integer(message["observed_generation"], "observed_generation", 1)
        signals = message["signals"]
        if not isinstance(signals, list) or not signals:
            raise ContractError("signals: expected a nonempty list")
        for signal in signals:
            _object(signal, {"evidence_id", "source", "reason", "detail", "at"}, path="signal")
            for key in {"evidence_id", "source", "reason", "detail"}:
                _string(signal[key], f"signal.{key}")
            _timestamp(signal["at"], "signal.at")

    elif kind == "proposal":
        action = message["action"]
        _enum(action, ACTION_TYPES, "action")
        _string(message["based_on_resource_version"], "based_on_resource_version")
        _string(message["rationale"], "rationale")
        _strings(message["evidence_refs"], "evidence_refs", 1)
        params = message["parameters"]
        if action == "ROLLBACK":
            _object(params, {"baseline_id", "template_sha256"}, path="parameters")
            _string(params["baseline_id"], "parameters.baseline_id")
            if not isinstance(params["template_sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", params["template_sha256"]):
                raise ContractError("parameters.template_sha256: expected a lowercase SHA-256 digest")
        elif action == "SCALE":
            _object(params, {"desired_replicas"}, path="parameters")
            _integer(params["desired_replicas"], "parameters.desired_replicas")
        elif action == "RESTART":
            _object(params, {"restart_token"}, path="parameters")
            _string(params["restart_token"], "parameters.restart_token")
        else:
            _object(params, set(), path="parameters")

    elif kind == "guard_result":
        _enum(message["verdict"], {"ALLOW", "REJECT", "ESCALATE"}, "verdict")
        _strings(message["reason_codes"], "reason_codes", 1)
        _string(message["policy_version"], "policy_version")
        _string(message["validated_resource_version"], "validated_resource_version")
        _timestamp(message["checked_at"], "checked_at")
        if version == "2.0":
            for key in ("proposal_sha256", "inputs_sha256"):
                if not isinstance(message[key], str) or not re.fullmatch(r"[a-f0-9]{64}", message[key]):
                    raise ContractError(f"{key}: expected a lowercase SHA-256 digest")
            if _timestamp(message["valid_until"], "valid_until") < _timestamp(message["checked_at"], "checked_at"):
                raise ContractError("valid_until: must not precede checked_at")

    elif kind == "dependency_result":
        _enum(message["status"], {"CLEAR", "BLOCKED", "UNKNOWN"}, "status")
        for key in {"affected_services", "blockers", "unknowns"}:
            _strings(message[key], key)
        _timestamp(message["checked_at"], "checked_at")

    elif kind == "execution_result":
        _enum(message["status"], {"APPLIED", "NOOP", "FAILED", "CONFLICT", "NEEDS_RECONCILIATION"}, "status")
        if message["resource_version"] is not None:
            _string(message["resource_version"], "resource_version")
        _timestamp(message["recorded_at"], "recorded_at")
        _string(message["details"], "details", allow_empty=True)

    elif kind == "outcome":
        _enum(message["status"], {"RECOVERED", "NOT_RECOVERED", "UNKNOWN"}, "status")
        start = _timestamp(message["window_start"], "window_start")
        end = _timestamp(message["window_end"], "window_end")
        if end < start:
            raise ContractError("window_end: must not precede window_start")
        checks = message["checks"]
        if not isinstance(checks, list) or not checks:
            raise ContractError("checks: expected a nonempty list")
        for check in checks:
            _object(check, {"name", "passed", "detail"}, path="check")
            _string(check["name"], "check.name")
            _string(check["detail"], "check.detail")
            if type(check["passed"]) is not bool:
                raise ContractError("check.passed: expected a boolean")
