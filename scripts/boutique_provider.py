"""Opt-in real-provider wiring for the isolated Boutique shipping experiment."""
import json
from pathlib import Path
import subprocess
import sys
import time

from runtime.executor_pipeline import process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance
from services.coordinator.core import handle
from services.planner.core import retrieve_runbooks
from services.planner.store import PlannerStore
from experiments.recovery.live import save, utc


def run_provider_case(lab, incident):
    folder = lab.directory / "provider-recovery"
    folder.mkdir(exist_ok=True)
    save(folder / "incident.json", incident)
    if lab.provider_policy:
        lab.policy = json.loads(lab.provider_policy.read_text(encoding="utf-8"))
        save(lab.directory / "policy.json", lab.policy)

    def provider(context):
        save(folder / "retrieved-context.json", context)
        result = subprocess.run([sys.executable, str(lab.provider_script)],
                                input=json.dumps(context), text=True, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("Planner provider adapter failed; no repair authorized")
        if len(result.stdout) > 65536:
            raise ValueError("Provider response exceeds 64 KiB")
        return json.loads(result.stdout)

    def plan():
        process_planner_requests(lab.store, lab.bus, baseline_reader=lambda i: [lab.baseline],
                                 retriever=retrieve_runbooks, provider=provider, journal=planning)
        record = planning.get(incident["incident_id"])
        if record:
            save(folder / "planning-status.json", {key: record[key] for key in
                 ("incident_id", "attempts", "next_attempt", "error_code", "proposal", "delivered")})

    def assess(proposal):
        save(folder / "proposal.json", proposal)
        finding = lab.dependency_result(proposal)
        save(folder / "dependencies.json", finding)
        handle(finding, lab.store, lab.bus)

    def guard():
        process_guard_requests(lab.store, lab.bus,
            state_reader=lambda p: {"cluster_id": lab.cluster_id, "captured_at": utc(),
                                    "deployment": lab.deployment()},
            baseline_reader=lambda p: lab.baseline, policy_reader=lambda p: lab.policy,
            dependency_reader=lab.dependency_result)

    def approve(proposal, decision):
        # The harness approver creates the approval through the authenticated API.
        return lab.request_approval(proposal, decision)

    def execute(proposal):
        save(folder / "approval.json", lab.store.approval(proposal["action_id"]))
        adapters = lab.adapters(folder, coordinator=True)
        try:
            # Re-evaluate dependencies under the executor's target lock on its current snapshot.
            adapters.authorizer.dependency_reader = lab.dependency_result
            results = process_executor_requests(lab.store, lab.bus, adapters=adapters)
            save(folder / "execution-results.json", results)
            save(folder / "transport-stats.json", adapters.cluster.stats)
        finally:
            adapters.ledger.close()

    with PlannerStore(folder / "planner.db") as planning:
        while True:
            state = advance(lab.store, incident["incident_id"], plan=plan, assess=assess, guard=guard,
                            approve=approve, execute=execute,
                            verify=lambda proposal, result: lab.complete_recovery(folder, proposal, result))
            if state != "OPEN":
                break
            time.sleep(1)  # Persisted bounded backoff; no fallback harness proposal.
    lab.summary.update(scope="Observed shipping incident through configured Planner provider and durable workers",
                       incident_id=incident["incident_id"], provider_state=state)
    if state != "RECOVERED":
        raise RuntimeError(f"Provider recovery stopped at {state}")
