"""Pure values shared by Guard checks; no cluster access or authorisation."""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import hashlib
import json
import math
import re

from shared.contracts import ContractError


class GuardInputError(ContractError):
    """An input cannot support a trustworthy Guard evaluation."""


def mapping(value, path):
    if not isinstance(value, dict):
        raise GuardInputError(f"{path}: expected an object")
    return value


def string(value, path):
    if not isinstance(value, str) or not value.strip():
        raise GuardInputError(f"{path}: expected a nonempty string")
    return value


def utc_timestamp(value, path):
    string(value, path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GuardInputError(f"{path}: invalid UTC timestamp") from exc
    if parsed.utcoffset() != timedelta(0):
        raise GuardInputError(f"{path}: expected a timezone-aware UTC timestamp")
    return parsed


def json_sha256(value):
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise GuardInputError("Fingerprint input must be JSON serializable") from exc
    return hashlib.sha256(encoded).hexdigest()


_NUMBER = r"\+?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
_CPU = re.compile(rf"({_NUMBER})(m|[eE][+-]?[0-9]+)?")
_MEMORY = re.compile(rf"({_NUMBER})(Ki|Mi|Gi|Ti|Pi|Ei|m|k|M|G|T|P|E|[eE][+-]?[0-9]+)?")
_MULTIPLIERS = {
    "": 1, "m": Fraction(1, 1000), "k": 1000, "M": 1000 ** 2,
    "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6,
    **{f"{prefix}i": 1024 ** index for index, prefix in enumerate("KMGTPE", 1)},
}
_MAX_QUANTITY = 2 ** 63 - 1


def quantity(value, resource, path):
    """Exact nonnegative CPU cores/memory bytes; reject rounding and huge inputs."""
    if resource not in ("cpu", "memory") or type(value) not in (str, int, float):
        raise GuardInputError(f"{path}: expected CPU or memory quantity")
    if type(value) is int and not 0 <= value <= _MAX_QUANTITY:
        raise GuardInputError(f"{path}: unsupported quantity magnitude")
    raw = str(value)
    if len(raw) > 256:
        raise GuardInputError(f"{path}: quantity token exceeds length limit")
    match = (_CPU if resource == "cpu" else _MEMORY).fullmatch(raw)
    if match is None:
        raise GuardInputError(f"{path}: invalid {resource} quantity")
    number, suffix = match.groups()
    suffix = suffix or ""
    exponent = suffix.startswith(("e", "E")) and len(suffix) > 1 and suffix != "Ei"
    try:
        decimal = Decimal(number + suffix if exponent else number)
    except InvalidOperation as exc:
        raise GuardInputError(f"{path}: invalid quantity") from exc
    if decimal > _MAX_QUANTITY * 1000 or (decimal != 0 and decimal < Decimal("1e-21")):
        raise GuardInputError(f"{path}: unsupported quantity magnitude")
    result = Fraction(decimal)
    if not exponent:
        result *= Fraction(1, 1000) if resource == "cpu" and suffix == "m" else (
            _MULTIPLIERS[suffix] if resource == "memory" else 1
        )
    if result > _MAX_QUANTITY:
        raise GuardInputError(f"{path}: quantity exceeds signed 64-bit magnitude")
    precision = result * 1000 if resource == "cpu" else result
    if precision.denominator != 1:
        raise GuardInputError(f"{path}: quantity cannot be rounded")
    return result


def _report_value(value, depth=0):
    if depth > 4:
        return "<nested value>"
    if isinstance(value, str):
        return value if len(value) <= 512 else value[:512] + "..."
    if type(value) is int and not -(2 ** 63) <= value <= 2 ** 63 - 1:
        return "<integer outside signed 64-bit range>"
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Fraction):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _report_value(item, depth + 1) for key, item in list(value.items())[:20]}
    if isinstance(value, (list, tuple)):
        return [_report_value(item, depth + 1) for item in value[:20]]
    return str(value)[:512]


def finding(code, path, actual, expected, message, status="FAIL"):
    """A bounded explanation. Callers supply metadata, never Secret contents."""
    if status not in ("PASS", "FAIL", "UNKNOWN", "SKIP"):
        raise ValueError("Invalid check status")
    return {
        "code": code, "status": status, "path": path,
        "actual": _report_value(actual), "expected": _report_value(expected),
        "message": _report_value(message),
    }
