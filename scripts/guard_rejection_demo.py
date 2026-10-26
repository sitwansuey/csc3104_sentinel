"""Inject a labelled false alarm and prove the Guard prevents a redundant write.

Preparation, health probes and RBAC setup happen before the measured refusal.
The injected failure is an incorrect recovery recommendation, not a Pod crash.
"""

from copy import deepcopy
import json
from uuid import uuid4

from runtime.guard_pipeline import process_guard_requests
from scripts.test_executor_live import now, require, save, utc
from services.coordinator import handle
from services.executor import execute
from services.executor_store import fingerprint
from services.guard import evaluate_with_report


REASON = "ROLLBACK_ALREADY_AT_BASELINE"
UNCHANGED_FIELDS = {"uid", "resource_version", "generation", "template_sha256", "pod_uids"}


def require_valid_approval(report):
    """A hard rejection must not conceal a broken approval in demo evidence."""
    checks = report["checks"]
    require(any(item["code"] == "APPROVAL_CHECK" and item["status"] == "PASS" for item in checks)
            and not any(("APPROVAL" in item["code"] or item["code"] == "OPERATOR_REJECTED")
                        and item["status"] in {"FAIL", "UNKNOWN"} for item in checks),
            "The independent rejection test requires a valid approval, not an approval failure")


def validate_rejection_proof(proof, *, incident_id, action_id, target_uid):
    """Reject incomplete or cross-incident zero-write evidence before displaying it."""
    require(proof.get("incident_id") == incident_id and proof.get("action_id") == action_id,
            "Zero-write proof belongs to another incident or action")
    require(proof.get("guard_verdict") == proof.get("approved_guard_verdict") == "REJECT"
            and proof.get("reason_code") == REASON and proof.get("executor_status") == "FAILED",
            "Proof must identify the same unnecessary-rollback refusal")
    require(all(type(proof.get(key)) is int and proof[key] == 0 for key in (
        "executor_patch_attempts", "executor_journal_attempts", "executor_routes")),
        "Zero-write proof must contain measured zero counters")
    unchanged, before, after = proof.get("unchanged"), proof.get("before"), proof.get("after")
    require(isinstance(unchanged, dict) and set(unchanged) == UNCHANGED_FIELDS
            and all(value is True for value in unchanged.values()), "Zero-write proof is incomplete")
    require(isinstance(before, dict) and isinstance(after, dict)
            and UNCHANGED_FIELDS <= before.keys() and UNCHANGED_FIELDS <= after.keys()
            and all(before[key] == after[key] for key in UNCHANGED_FIELDS)
            and before["uid"] == target_uid,
            "Before/after workload identity or configuration does not match")
    require(all(type(item.get("desired")) is int and item["desired"] > 0
                and item.get("ready") == item.get("available") == item["desired"] for item in (before, after)),
            "The healthy service did not remain ready")


def workload_proof(experiment):
    """Read only workload identity, template hash and health; never credentials."""
    deployment = experiment.deployment()
    metadata, status = deployment["metadata"], deployment.get("status", {})
    pods = json.loads(experiment.kubectl(
        "get", "pods", "-l", "app=" + experiment.name, "-o", "json").stdout)
    return {
        "uid": metadata["uid"], "resource_version": metadata["resourceVersion"],
        "generation": metadata["generation"],
        "template_sha256": fingerprint(deployment["spec"]["template"]),
        "desired": deployment["spec"].get("replicas", 1),
        "ready": status.get("readyReplicas", 0), "available": status.get("availableReplicas", 0),
        "observed_generation": status.get("observedGeneration", 0),
        "pod_uids": sorted(pod["metadata"]["uid"] for pod in pods["items"]),
    }


def run_unnecessary_rollback(experiment):
    """Use the real Coordinator, Guard and restricted Executor on a healthy lab."""
    if experiment.pause_before_injection:
        input("Healthy baseline ready. Press Enter to inject an unnecessary rollback proposal "
              "(the service remains healthy; Ctrl+C aborts): ")
    before = workload_proof(experiment)
    require(before["desired"] > 0 and before["ready"] == before["available"] == before["desired"]
            and before["observed_generation"] >= before["generation"],
            "Rejection demo requires a currently ready and available service")
    require(before["template_sha256"] == experiment.baseline["template_sha256"],
            "Rejection demo requires the actual healthy baseline template")
    observed_at = utc()
    incident = {
        "schema_version": "1.0", "kind": "incident",
        "incident_id": "INC-FALSE-ALARM-" + uuid4().hex[:12].upper(),
        "target": deepcopy(experiment.target), "observed_at": observed_at,
        "observed_resource_version": before["resource_version"],
        "observed_generation": before["generation"],
        "signals": [{"evidence_id": "INJECTED-" + uuid4().hex[:12],
                     "source": "demo_harness", "reason": "InjectedFalseAlarm",
                     "detail": "Injected false alarm: shippingservice is healthy and already uses the "
                               "requested baseline. This tests an incorrect rollback recommendation; "
                               "the Observer did not detect a real Pod failure.", "at": observed_at}],
    }
    handle(incident, experiment.store, experiment.bus)
    experiment.event(3, "fault", "Injected false recovery recommendation; no Pod failure was injected",
                     target=experiment.target, incident_id=incident["incident_id"], at=observed_at)
    save(experiment.directory / "false-alarm-incident.json", incident)
    folder, proposal = experiment.case(
        "unnecessary-rollback", incident,
        rationale="Operator-authored fault injection: deliberately recommend a redundant rollback "
                  "while the service already runs its measured healthy template. The Guard must refuse.")
    handle(proposal, experiment.store, experiment.bus)
    dependencies = json.loads((folder / "dependencies.json").read_text(encoding="utf-8"))
    handle(dependencies, experiment.store, experiment.bus)
    decisions = process_guard_requests(
        experiment.store, experiment.bus,
        state_reader=lambda p: {"cluster_id": experiment.cluster_id, "captured_at": utc(),
                                "deployment": experiment.deployment()},
        policy_reader=lambda p: experiment.policy, baseline_reader=lambda p: experiment.baseline,
        dependency_reader=lambda p: dependencies)
    require(len(decisions) == 1 and decisions[0]["verdict"] == "REJECT"
            and REASON in decisions[0]["reason_codes"], "Guard did not reject the unnecessary rollback")
    decision = decisions[0]
    save(folder / "guard-decision.json", decision)
    require(experiment.store.incident(incident["incident_id"])["state"] == "REJECTED",
            "Coordinator did not record the rejected recommendation")
    routes = experiment.store.db.execute(
        "SELECT COUNT(*) FROM outbox WHERE action_id=? AND topic='executor.request'",
        (proposal["action_id"],)).fetchone()[0]
    require(routes == 0, "Rejected proposal unexpectedly scheduled an Executor request")

    # Independently try the standard Executor with a valid, proposal-bound lab
    # approval. No Coordinator route is manufactured for this negative test.
    adapters = experiment.adapters(folder)
    try:
        snapshot = adapters.cluster.read(proposal["target"])
        check = adapters.authorizer.validate(proposal, snapshot, now=now())
        approved_decision, approved_report = evaluate_with_report(**check.inputs)
        require(approved_decision["verdict"] == "REJECT"
                and REASON in approved_decision["reason_codes"],
                "Approval incorrectly overrode unnecessary-rollback rejection")
        require_valid_approval(approved_report)
        save(folder / "approved-guard-report.json", approved_report)
        save(folder / "approved-guard-decision.json", approved_decision)
        result = execute(proposal, adapters)
        entry = adapters.ledger.get(proposal["action_id"])
        require(result["status"] == "FAILED" and entry["guard_result"]["verdict"] == "REJECT"
                and REASON in entry["guard_result"]["reason_codes"],
                "Restricted Executor did not enforce the fresh Guard rejection")
        require(adapters.cluster.stats["write_attempts"] == 0 and entry["attempts"] == 0,
                "Unnecessary rollback attempted a Kubernetes write")
        save(folder / "execution.json", result)
        save(folder / "transport-stats.json", adapters.cluster.stats)
        after = workload_proof(experiment)
        unchanged = {key: before[key] == after[key] for key in (
            "uid", "resource_version", "generation", "template_sha256", "pod_uids")}
        require(all(unchanged.values()), "Workload changed during the measured refusal")
        require(after["desired"] == after["ready"] == after["available"] and after["desired"] > 0,
                "Service did not remain ready after refusal")
        proof = {
            "scenario": "injected false recovery recommendation; no Pod failure",
            "incident_id": incident["incident_id"], "action_id": proposal["action_id"],
            "guard_verdict": decision["verdict"], "reason_code": REASON,
            "approved_guard_verdict": approved_decision["verdict"],
            "executor_status": result["status"], "executor_patch_attempts": adapters.cluster.stats["write_attempts"],
            "executor_journal_attempts": entry["attempts"], "executor_routes": routes,
            "before": before, "after": after, "unchanged": unchanged,
            "measurement_scope": "After baseline/probe/RBAC setup; counts Executor recovery writes only.",
        }
        validate_rejection_proof(proof, incident_id=incident["incident_id"],
                                 action_id=proposal["action_id"], target_uid=experiment.target["uid"])
        save(folder / "proof.json", proof)
    finally:
        adapters.ledger.close()
    experiment.event(4, "status", "Guard REJECT: the requested rollback template already matches "
                     "the running template; this modification is unnecessary.", target=experiment.target,
                     incident_id=incident["incident_id"], action_id=proposal["action_id"])
    experiment.event(5, "status", "Independent restricted Executor test with valid lab approval: "
                     "0 patch attempts; Deployment template, version, generation and workload Pod UIDs unchanged. "
                     "No execution was scheduled by Coordinator.", target=experiment.target,
                     incident_id=incident["incident_id"], action_id=proposal["action_id"])
    experiment.observe("rejection-after")
    experiment.summary["guard_rejection"] = proof
    experiment.check("Guard rejects unnecessary rollback despite approval; zero Executor patch attempts",
                     verdict="REJECT", reason_code=REASON, executor_patch_attempts=0)
    return proof
