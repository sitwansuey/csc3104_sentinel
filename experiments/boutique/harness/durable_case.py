"""One observed Boutique incident through the durable Planner journal and the guarded workers.

Shared by subprocess-provider mode (scripts/boutique_provider.py) and agent mode
(experiments/boutique/harness/agent_live.py), so both plan, retry, escalate and
replay the same way. The journal is <folder>/planner.db. A proposal is saved
there before it is delivered, so a restart with the same folder replays it
without planning again.
"""
import time

from runtime.executor_pipeline import process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance
from services.coordinator.core import handle
from services.planner.store import PlannerStore
from experiments.recovery.live import save, utc

MAX_ATTEMPTS = 3
RETRY_SECONDS = 5
POLL_SECONDS = 1
STATUS_FIELDS = ("incident_id", "attempts", "next_attempt", "error_code", "proposal", "delivered")


def planning_status(journal, incident_id):
    """The journal row without exception text, plus each attempt's recorded usage and timings."""
    record = journal.get(incident_id)
    if record is None:
        return None
    status = {key: record[key] for key in STATUS_FIELDS}
    status["attempt_usage"] = [{"attempt": item["attempt"], "usage": item["usage"]}
                               for item in journal.attempts(incident_id)]
    return status


def run_durable_case(lab, incident, *, folder, planning, operator):
    """Advance the incident until it leaves OPEN and return its Coordinator state.

    planning holds the process_planner_requests() keywords for the provider
    (baseline_reader, retriever, provider, and for agent mode diagnoser,
    finalize and lease_seconds). operator(proposal) returns an approving
    identity or None. A planning failure ends in a saved ESCALATE, so the loop
    ends in NEEDS_REVIEW instead of leaving the incident OPEN.
    """
    folder.mkdir(exist_ok=True)
    save(folder / "incident.json", incident)
    incident_id = incident["incident_id"]

    def plan():
        process_planner_requests(lab.store, lab.bus, journal=journal, max_attempts=MAX_ATTEMPTS,
                                 retry_seconds=RETRY_SECONDS, **planning)
        status = planning_status(journal, incident_id)
        if status:
            save(folder / "planning-status.json", status)
            if status["delivered"] and not (folder / "proposal.json").exists():
                save(folder / "proposal.json", status["proposal"])
                lab.phase("proposal_saved", folder.name)

    def assess(proposal):
        finding = lab.dependency_result(proposal)
        save(folder / "dependencies.json", finding)
        handle(finding, lab.store, lab.bus)

    def guard():
        decisions = process_guard_requests(lab.store, lab.bus,
            state_reader=lambda p: {"cluster_id": lab.cluster_id, "captured_at": utc(),
                                    "deployment": lab.deployment()},
            baseline_reader=lambda p: lab.baseline, policy_reader=lambda p: lab.policy,
            dependency_reader=lab.dependency_result)
        for decision in decisions:
            save(folder / "initial-guard.json", decision)
            if decision["reason_codes"] != ["OPERATOR_APPROVAL_REQUIRED"]:
                lab.phase("guard_decision", folder.name)

    def execute(proposal):
        save(folder / "approval.json", lab.store.approval(proposal["action_id"]))
        adapters = lab.adapters(folder, coordinator=True)
        try:
            # Re-evaluate dependencies under the executor's target lock on its current snapshot.
            adapters.authorizer.dependency_reader = lab.dependency_result
            results = process_executor_requests(lab.store, lab.bus, adapters=adapters)
            save(folder / "execution-results.json", results)
            save(folder / "transport-stats.json", adapters.cluster.stats)
            for result in results:
                if result["action_id"] == proposal["action_id"]:
                    save(folder / "execution.json", result)
                    lab.phase("execution_recorded", folder.name)
        finally:
            adapters.ledger.close()

    with PlannerStore(folder / "planner.db") as journal:
        while True:
            state = advance(lab.store, incident_id, plan=plan, assess=assess, guard=guard,
                            operator=operator, execute=execute,
                            verify=lambda proposal, result: lab.complete_recovery(folder, proposal, result))
            if state != "OPEN":
                return state
            time.sleep(POLL_SECONDS)  # Persisted bounded backoff; no fallback harness proposal.
