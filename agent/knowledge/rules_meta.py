"""Kr: Invariants, preconditions, and causal rules in RCAFlow."""
from typing import List

GLOBAL_RULES: List[str] = [
    "Compute metric baselines using pre-incident data before applying time-window filters.",
    "Preserve component IDs, service names, and container IDs exactly as they appear in the system context.",
    "Timestamp strings must follow 'YYYY-MM-DD HH:MM:SS' in Asia/Shanghai (UTC+8) timezone.",
    "The earliest outlier anomaly is NOT necessarily the root cause; distinguish initiating faults from downstream symptoms.",
    "Require persistent onset (at least 3 samples spanning >= 2 cadences) and corroborating multi-modal evidence.",
    "Missing or truncated telemetry data must lower confidence rather than produce hallucinated answers.",
    "Routine Young GC ('GC Allocation Failure') is normal JVM operation and must not be reported as a failure.",
    "Trace latency spikes alone cannot prove network or service failure without corresponding server-side error logs or metric saturation."
]


def get_global_rules() -> List[str]:
    return list(GLOBAL_RULES)

