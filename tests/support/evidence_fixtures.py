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


def preflight_run(root, name, *, injected=False, started="2026-11-11T10:10:00Z"):
    """An agent run stopped by the model preflight; injected=True marks a fault already in place."""
    run = Path(root) / name
    error = "Ollama preflight failed for qwen3:missing at 127.0.0.1:11434: model is not installed"
    write(run / "summary.json", {"status": "FAILED", "started_at": started, "checks": [], "error": error,
                                 "error_type": "RuntimeError", "model": "qwen3:missing",
                                 "agent_failure": {"error": "RuntimeError: " + error, "fault_injected": injected,
                                                   "seconds": 0.1, "stage": "preflight"}})
    return run


def wedge_run(root, name, *, scenario="rv-race", fixed=True, started="2026-11-11T20:21:23Z"):
    """An Executor wedge harness folder: fixed=True resolves (SUPERSEDED, or CONFLICT/FENCED for admission-deny),
    False stays NEEDS_RECONCILIATION."""
    run = Path(root) / name
    basis = {"rv-race": "ADVANCED_WITHOUT_EVIDENCE", "delete-recreate": "TARGET_REPLACED",
             "admission-deny": "FENCED"}[scenario]
    resolved = "CONFLICT" if scenario == "admission-deny" else "SUPERSEDED"
    status = resolved if fixed else "NEEDS_RECONCILIATION"
    results = [{"label": "execute", "status": status if scenario == "rv-race" else None, "passes": 1},
               {"label": "reconcile", "status": status, "basis": basis if fixed else None, "passes": 5}]
    checks = [{"name": f"first action resolves as {resolved}/{basis}", "passed": fixed,
               "observed": [status, basis if fixed else None]},
              {"name": "ledger releases the workload", "passed": fixed},
              {"name": "a new proposal for the workload is admitted", "passed": fixed}]
    if fixed:
        results.append({"label": "second execute", "status": "APPLIED", "basis": "EVIDENCE_MATCH", "passes": 1})
        checks.append({"name": "the new proposal is APPLIED", "passed": True})
    write(run / (scenario + ".json"), {"scenario": scenario, "status": "PASS" if fixed else "FAIL",
                                       "started_at": started, "context": "kind-sentinel-wedge",
                                       "reconcile_pass_statuses": [status] * 5, "results": results,
                                       "checks": checks})
    write(run / "manifests.json", {"apiVersion": "v1", "kind": "List", "items": []})
    if scenario == "admission-deny":
        write(run / "admission-policy.json", {"apiVersion": "v1", "kind": "List", "items": []})
    write(run / "executor.sqlite", b"SQLite format 3\x00")
    write(run / "locks/target.lock", "")
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


def scenario_run(root, name, *, scenario="R2", status="PASS", actual="ROLLBACK", source="model", codes=(), writes=1,
                 state="RECOVERED", matched=True, error_code=None, expected="recovered", flagged_lines=0,
                 finding=None, started="2026-11-12T08:00:00Z"):
    """An agent run of a catalog scenario with its scenario-result.json and one logs-tool branch result."""
    run = Path(root) / name
    result = {"scenario": scenario, "actual": actual, "decision_source": source, "decided_by": source if source in (
        "rule", "model") else None, "eligibility_codes": list(codes), "executor_writes": writes,
        "coordinator_state": state, "matched_expected": matched, "planning_error_code": error_code,
        "labels": {"status": "pending", "by": None, "on": None}, "labels_pending": True}
    summary = {"status": status, "started_at": started, "checks": [], "expected_outcome": expected,
               "scenario": {"id": scenario, "labels": "pending"}, "scenario_result": result}
    if error_code:
        summary["agent_failure"] = {"error": f"ESCALATE {error_code} after 1 attempt(s)", "error_code": error_code}
    if finding is not None:
        summary["injection_finding"] = finding
    write(run / "summary.json", summary)
    write(run / "scenario-result.json", result)
    write(run / "approved-rollback/planning-status.json", {"attempts": 1, "error_code": error_code})
    write(run / "approved-rollback/attempt-1/agent/report.json", {"failures": []})
    write(run / "approved-rollback/attempt-1/agent/branch-003/result.json", {"state": "success", "outputs": [
        {"evidence_id": "branch-003", "tool": "logs", "result": {"suspected_injection_lines": flagged_lines}}]})
    write(run / "approved-rollback/attempt-1/agent/branch-003/task.json", {"tool": "logs"})
    write(run / "approved-rollback/attempt-1/telemetry/logs.csv", "timestamp,message\n")
    if writes:
        write(run / "approved-rollback/execution.json", {"status": "APPLIED"})
    return run
