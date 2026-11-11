"""Opt-in real-provider wiring for the isolated Boutique shipping experiment."""
import json
import subprocess
import sys

from services.planner.core import retrieve_runbooks
from experiments.boutique.harness.durable_case import run_durable_case
from experiments.recovery.live import save


def run_provider_case(lab, incident):
    folder = lab.directory / "provider-recovery"
    folder.mkdir(exist_ok=True)
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

    def operator(proposal):
        action_id = proposal["action_id"]
        if lab.operator and input(f"To approve rollback, enter {action_id}: ").strip() == action_id:
            return lab.operator
        return None

    state = run_durable_case(lab, incident, folder=folder, operator=operator, planning={
        "baseline_reader": lambda i: [lab.baseline], "retriever": retrieve_runbooks, "provider": provider})
    lab.summary.update(scope="Observed shipping incident through configured Planner provider and durable workers",
                       incident_id=incident["incident_id"], provider_state=state)
    if state != "RECOVERED":
        raise RuntimeError(f"Provider recovery stopped at {state}")
