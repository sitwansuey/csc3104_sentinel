"""Offline worker lifecycle demo with temporary databases and a replay clock."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import LocalEventBus, SQLiteStore, approve, handle
from services.guard.core import evaluate
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore


def load(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


def run_scenario(name):
    with tempfile.TemporaryDirectory() as directory:
        cp, vp = Path(directory) / "coordinator.sqlite3", Path(directory) / "verifier.sqlite3"
        clock = datetime(2026, 10, 10, 12, 0, 36, tzinfo=timezone.utc)
        coordinator = SQLiteStore(cp, clock=lambda: clock)
        ledger = VerificationStore(vp)
        bus = LocalEventBus()
        proposal, execution = load("messages/proposal.json"), load("messages/execution-result.json")
        policy, state, baseline = load("policy.json"), load("observations.json")["failed-rollout"][-1], load("baseline.json")
        rows = [] if name == "timeout" else load("health.json")["unstable" if name == "unhealthy" else "stable"]
        try:
            for message in (load("messages/incident.json"), proposal):
                handle(message, coordinator, bus)
            dependencies = inspect_dependencies(proposal, load("dependencies.json"), state, now=clock)
            handle(dependencies, coordinator, bus)
            handle(evaluate(proposal, state, policy, baseline, dependencies, now=clock), coordinator, bus)
            approval = approve(proposal["action_id"], "offline-demo-operator", coordinator)
            clock += timedelta(seconds=4)
            handle(evaluate(proposal, state, policy, baseline, dependencies, approval, now=clock), coordinator, bus)
            handle(execution, coordinator, bus)
            history = []
            seconds = (50, 60, 70) if name != "timeout" else (50, 115)
            for index, second in enumerate(seconds):
                clock = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=second)
                available = [row for row in rows if datetime.fromisoformat(row["at"].replace("Z", "+00:00")) <= clock]
                process_verifier_requests(coordinator, bus, ledger=ledger,
                    policy_reader=lambda execution: policy, sample_reader=lambda execution: available, clock=lambda: clock)
                history.append(coordinator.incident(proposal["incident_id"])["state"])
                if name == "restart" and index == 0:
                    coordinator.close()
                    ledger.close()
                    coordinator, ledger = SQLiteStore(cp), VerificationStore(vp)
            job = ledger.get(proposal["action_id"])
            return {"states": history, "outcome": job["outcome"]["status"],
                    "saved_samples": len(job["samples"]), "outcome_delivered": bool(job["delivered"])}
        finally:
            coordinator.close()
            ledger.close()


def main():
    results = {name: run_scenario(name) for name in ("healthy", "unhealthy", "timeout", "restart")}
    print(json.dumps({"mode": "synthetic_offline", "results": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
