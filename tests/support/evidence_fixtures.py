"""Small fake run folders for the evidence collection and summary tests."""

import json
from pathlib import Path

from scripts.evidence.sanitise import Context

REVISION = "0123456789abcdef0123456789abcdef01234567"


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (bytes, bytearray)):
        path.write_bytes(value)
    else:
        path.write_text(value if isinstance(value, str) else json.dumps(value, indent=2), encoding="utf-8",
                        newline="\n")
    return path


def context():
    """A sanitiser context that does not depend on the machine running the tests."""
    return Context(repo_root="C:\\work\\repo", home="C:\\Users\\tester", usernames=("tester",))


def check(name, passed=True):
    return {"name": name, "passed": passed, "at": "2026-11-11T10:00:00Z"}


def agent_run(root, name, *, status="PASS", started="2026-11-11T10:00:00Z", error=None, calls=3, seconds=(20, 10, 8),
              timeline=None, provenance=None):
    run = Path(root) / name
    summary = {"status": status, "started_at": started, "planning": "Live RCA agent and Ollama Recovery Planner",
               "model": "qwen3:14b", "checks": [check("healthy baseline captured")]}
    if status == "PASS":
        summary["agent_model_calls"] = calls
        summary["checks"].append(check("Role 6 verified gRPC, readiness and template; Coordinator RECOVERED"))
        write(run / "primary-incident.json", {"observed_at": "2026-11-11T10:01:00Z"})
        write(run / "approved-rollback/execution.json", {"status": "APPLIED", "recorded_at": "2026-11-11T10:01:45Z"})
        write(run / "approved-rollback/outcome.json", {"status": "RECOVERED", "window_end": "2026-11-11T10:02:40Z"})
        write(run / "approved-rollback/model-calls.json", [{"seconds": s, "role": "r"} for s in seconds])
    if error:
        summary.update(error_type="RuntimeError", error=error, agent_failure={"error": error, "model_calls": 0})
    if timeline is not None:
        summary["timeline"] = timeline
    if provenance is not None:
        summary["provenance"] = provenance
    write(run / "summary.json", summary)
    return run


def connected_run(root, name, *, started="2026-11-11T09:00:00Z", passed=True):
    run = Path(root) / name
    checks = [check("healthy baseline captured"), check("approved real rollback"),
              check("crash reconciles without resubmission", passed)]
    write(run / "summary.json", {"status": "PASS" if passed else "FAILED", "started_at": started, "checks": checks})
    return run


def rejection_run(root, name, *, writes=0):
    run = Path(root) / name
    write(run / "summary.json", {"status": "PASS", "started_at": "2026-11-11T11:00:00Z", "checks": [],
                                 "guard_rejection": {"guard_verdict": "REJECT", "executor_patch_attempts": writes}})
    return run


def chaos_run(root, name, *, faults=("network", "pod"), runs=None, status="complete"):
    run = Path(root) / name
    write(run / "batch.json", {"started_at": "2026-11-10T20:00:00Z", "status": status,
                               "configuration": {"faults": list(faults)}, "runs": runs if runs is not None else [
                                   {"run_id": f"000{i + 1}-{fault}-abc", "injection_confirmed": True,
                                    "cleanup_confirmed": True, "recovery_healthy": fault != "pod"}
                                   for i, fault in enumerate(faults)]})
    return run


def security_run(root, name, *, passed=True):
    run = Path(root) / name
    probes = {probe: {"succeeded": False, "output": "denied"} for probe in (
        "invalid_quantity", "missing_client_certificate", "wrong_client_identity", "direct_service_bypass",
        "direct_pod_ip_bypass", "direct_egress_bypass", "proxy_outage", "outage_direct_bypass")}
    write(run / "validation.json", {"baseline_attempts": [{"at": "2026-11-11T15:00:00Z"}], "passed": passed,
                                    "valid_mtls_quote": {"succeeded": True},
                                    "shopping_after_proxy_restore": {"passed": passed}, **probes})
    return run


def observer_run(root, name):
    run = Path(root) / name
    write(run / "summary.json", {"status": "PASS", "started_at": "2026-11-11T14:00:00Z",
                                 "detection_elapsed_seconds": 7.42, "duplicate_suppression_seconds": 61.9})
    return run
