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


def forced(action, *, verdict="REJECT", codes=(), status="FAILED", writes=0, label=None, expected=None):
    """One forced wrong action as a chaos run's summary records it."""
    item = {"action": action, "guard_verdict": verdict, "guard_reason_codes": list(codes), "executor_status": status,
            "executor_patch_attempts": writes, "executor_journal_attempts": writes}
    if label:
        item["label"] = label
    if expected:
        item["expected_reason"] = expected
    return item


def chaos_recovery_run(root, name, *, scenario="N2", kind="PodChaos", status="PASS", actual="RESTART", writes=1,
                       attributed=True, refusals=None, cleanup=True, started="2026-11-13T09:00:00Z"):
    """A connected catalog run with a Chaos Mesh fault, harness planning and forced wrong actions."""
    run = Path(root) / name
    result = {"scenario": scenario, "actual": actual, "decision_source": "rule", "decided_by": "rule",
              "eligibility_codes": [], "executor_writes": writes,
              "coordinator_state": None if actual == "NO_INCIDENT" else "RECOVERED" if writes else "NEEDS_REVIEW",
              "matched_expected": True,
              "labels": {"status": "pending", "by": None, "on": None}, "labels_pending": True}
    summary = {"status": status, "started_at": started, "checks": [], "scenario": {"id": scenario, "labels": "pending"},
               "scenario_result": result, "chaos": {"kind": kind, "name": "sentinel-x"},
               "chaos_cleanup_confirmed": cleanup,
               "attribution": None if attributed is None else {"attributed": attributed, "failed": []},
               "forced_refusals": refusals}
    write(run / "summary.json", summary)
    write(run / "scenario-result.json", result)
    write(run / "chaos-experiment.json", {"kind": kind})
    write(run / "chaos-recovery/proposal.json", {"action": actual})
    write(run / "chaos-recovery/approval.json", {"signature": "never collected"})
    write(run / "forced-rollback/proof.json", {"executor_patch_attempts": 0})
    if actual == "NO_INCIDENT":
        write(run / "true-negative.json", {"passed": True, "executor_writes": 0})
    write(run / "executor.sqlite", b"never collected")
    return run


# The file layout of a real scenario-pair run folder (redis-pod+cart-config, 13 November 2026), which stopped after
# detection: probe Pods, observations, the Observer, metrics and telemetry stores, the Coordinator database, the
# connected manifest and RBAC, baselines, policies and each target's baseline health.
PAIR_RUN_LAYOUT = (
    "allowed-targets.json", "baseline-storefront.json", "cart-probe-1a35386cd3.json", "cart-probe-1f8ccf4d81.json",
    "cartservice-healthy-0-observation.json", "cartservice-pair-0-observation.json",
    "cartservice-pair-2-incident.json", "cartservice-pair-2-observation.json", "chaos-experiment.json",
    "chaos-request.json", "connected-manifest.json", "coordinator.sqlite", "dependency-graph.json",
    "emergency-cleanup-patch.json", "executor-rbac.yaml", "images.json", "metrics-bridge.json",
    "observer-cartservice.sqlite", "observer-metrics.sqlite3", "observer-redis-cart.sqlite", "probe-proto.json",
    "rbac-checks.json", "redis-cart-healthy-0-observation.json", "redis-cart-pair-0-observation.json",
    "redis-cart-pair-2-incident.json", "redis-cart-pair-2-observation.json", "redis-probe-bf847b5d13.json",
    "storefront-cleanup.json", "storefront-forward.log", "summary.json", "telemetry.sqlite3",
    "baselines/cartservice.json", "baselines/redis-cart.json", "pair-cartservice/baseline-health.json",
    "pair-redis-cart/baseline-health.json", "policies/cartservice.json", "policies/redis-cart.json")
# What multi_fault.py adds once a pair is judged: the pair result and, per target, its proposal, Guard decisions,
# dependency findings, approval, execution, outcome and score; the Executor-kill variant's worker files; the
# Executor, Planner and Verifier stores; and the private Executor kubeconfig while the run is still going.
PAIR_RUN_JUDGED = (
    ".executor-kubeconfig.json", "pair-result.json", "pair-storefront-impact.json", "attribution.json",
    "dependency-readiness-cartservice.json", "dependency-readiness-redis-cart.json", "executor.sqlite",
    "planner.db", "verifier.sqlite3", "locks/target.lock", "operator-cleanup-cartservice-patch.json",
    "pair-redis-cart/proposal-ACT-R.json", "pair-redis-cart/dependencies-ACT-R.json",
    "pair-redis-cart/guard-ACT-R-0.json", "pair-redis-cart/approval.json", "pair-redis-cart/dependencies.json",
    "pair-redis-cart/execution-ACT-R.json", "pair-redis-cart/outcome-ACT-R.json", "pair-redis-cart/outcome.json",
    "pair-redis-cart/post-health-ACT-R.json", "pair-redis-cart/scenario-result.json",
    "pair-redis-cart/final-deployment.json", "pair-cartservice/proposal-ACT-C.json",
    "pair-cartservice/guard-ACT-C-0.json", "pair-cartservice/replan-1.json", "pair-cartservice/proposal.json",
    "pair-cartservice/worker.json", "pair-cartservice/write-receipt.json", "pair-cartservice/reconciliation.json",
    "pair-cartservice/scenario-result.json", "pair-cartservice/final-deployment.json")


def pair_run(root, name, *, status="PASS", result="DEPENDENCY_DEADLOCK", shop="FAILED", started="2026-11-13T09:50:52Z",
             judged=True, labels="pending"):
    """A scenario-pair run folder with the real layout; judged adds what a run that reached judging writes."""
    run = Path(root) / name
    for relative in PAIR_RUN_LAYOUT + (PAIR_RUN_JUDGED if judged else ()):
        write(run / relative, {"file": relative})
    stops = ({"redis-cart": "DEPENDENCY_DEADLOCK", "cartservice": "DEPENDENCY_DEADLOCK"}
             if result == "DEPENDENCY_DEADLOCK" else {})
    writes = {"redis-cart": 0, "cartservice": 0} if result == "DEPENDENCY_DEADLOCK" else {"redis-cart": 1,
                                                                                          "cartservice": 1}
    graded = {"pair": "redis-pod+cart-config", "result": result, "writes": writes, "stops": stops,
              "labels": labels, "labels_pending": labels != "signed",
              "ordered_recovery_shown": result == "RECOVERED"}
    summary = {"status": status, "started_at": started, "checks": [],
               "pair": {"id": "redis-pod+cart-config", "kind": "dependency_ordered", "labels": labels},
               "provenance": {"source_revision": REVISION, "dirty": False}}
    if judged:
        summary["pair_result"] = {"result": graded, "shop": {"status": shop}}
        write(run / "pair-result.json", {"result": graded, "shop": {"status": shop}})
    write(run / "summary.json", summary)
    return run


def jsonl(path, records):
    """Write one JSON object per line, as the distributed harness and the operator orchestrators do."""
    return write(path, "".join(json.dumps(record) + "\n" for record in records))


def span(name, span_id, parent=None, *, process="coordinator", ts="2026-11-12T06:44:00.000Z", ms=100.0,
         trace="t" * 32, **extra):
    record = {"process": process, "ts": ts, "event": "span.end", "trace_id": trace, "span_id": span_id,
              "span_name": name, "duration_ms": ms, **extra}
    if parent:
        record["parent_span_id"] = parent
    return record


def distributed_run(root, name, *, scenario="S1b", mode="kind", matches=True, failed=(), states=("RECOVERED",),
                    writes=1, timeline=(), notes=None, started="2026-11-12T17:55:29.797541Z", visible=None):
    """One run_scenarios run folder: summary.json, timeline.jsonl and a coordinator-records.json."""
    run = Path(root) / name
    invariants = [{"id": key, "passed": key not in failed, "detail": "fixture"} for key in ("I1", "I3", "I5")]
    summary = {"schema": "sentinel-distributed-run-v1", "scenario": scenario, "mode": mode, "started_at": started,
               "duration_seconds": 50.8, "deadline_seconds": 180, "git_revision": REVISION, "harness_error": None,
               "states": list(states), "committed_writes": writes, "unobserved_windows": 0,
               "matches_expectation": matches, "invariants": invariants, "notes": notes or {}}
    if visible is not None:
        summary["kind_lab"] = {"fault": {"visible_after_seconds": visible}}
    write(run / "summary.json", summary)
    jsonl(run / "timeline.jsonl", list(timeline))
    write(run / "coordinator-records.json", {"receipts": []})
    write(run / "coordinator.sqlite", b"SQLite format 3\x00")
    return run


def admission_run(root, name, *, failing=()):
    """A live admission-policy check: two allowed, two policy denials, one RBAC denial and two non-probe checks."""
    run = Path(root) / name
    probes = [("server fields", None), ("ROLLBACK", "ALLOWED"), ("seal 5", "ALLOWED"), ("replicas", "DENIED_BY_POLICY"),
              ("lower epoch", "DENIED_BY_POLICY"), ("scale", "DENIED_BY_RBAC"), ("final state", None)]
    checks = [{"name": label, "expected": expected, "passed": label not in failing, "rule_named": True,
               "object_unchanged": expected != "ALLOWED"} for label, expected in probes]
    write(run / "admission.json", {"status": "FAIL" if failing else "PASS", "started_at": "2026-11-12T13:59:24Z",
                                   "server_version": "1.37", "checks": checks, "namespace_deleted": True,
                                   "policy_deleted": True, "private_credentials_removed": True})
    return run


def docker_state(status, exit_codes):
    return [{"State": {"Status": "running", "Health": {"Status": status, "FailingStreak": 0, "Log": [
        {"Start": "2026-11-12T18:00:00Z", "ExitCode": code, "Output": ""} for code in exit_codes]}}}]


def container_run(root, name, *, orchestrated=True, unready_sample=False):
    """A worker container run: Docker state and a planner log, plus the orchestrator's records when orchestrated."""
    run = Path(root) / name
    write(run / "planner-container.state.json", docker_state("unhealthy", (0, 0, 1, 1, 1)))
    end = {"event": "span.end", "span_name": "worker.process", "topic": "planner.request", "duration_ms": 89604.657}
    write(run / "planner-container.log", "starting\n2026-11-12T18:12:28Z " + json.dumps(end) + "\n")
    if orchestrated:
        jsonl(run / "events.jsonl", [{"utc": "2026-11-12T18:10:36Z", "event": "container_started"},
                                     {"utc": "2026-11-12T18:12:28Z", "event": "result"},
                                     {"utc": "2026-11-12T18:26:22Z", "event": "coordinator_stopped"},
                                     {"utc": "2026-11-12T18:27:24Z", "event": "readyz_503",
                                      "after_stop_seconds": 62.0},
                                     {"utc": "2026-11-12T18:28:28Z", "event": "docker_unhealthy",
                                      "after_stop_seconds": 126.0}])
        ready = lambda age: "200 " + json.dumps({"contact_age_seconds": age})
        jsonl(run / "health.jsonl", [
            {"utc": "2026-11-12T18:10:40Z", "health": "starting", "readyz": "503 {}"},
            {"utc": "2026-11-12T18:10:49Z", "health": "healthy", "readyz": ready(1.5)},
            {"utc": "2026-11-12T18:11:30Z", "health": "healthy",
             "readyz": "503 {}" if unready_sample else ready(4.66)},
            {"utc": "2026-11-12T18:24:33Z", "health": "healthy", "readyz": ready(19.062)},
            {"utc": "2026-11-12T18:27:30Z", "health": "healthy", "readyz": "503 {}"}])
    return run
