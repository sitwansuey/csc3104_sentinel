"""Offline coordinator integration: real Observer/Guard, synthetic other services."""

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.coordinator.core import LocalEventBus, SQLiteStore, acknowledge, approve, dispatch_pending, handle
from runtime.guard_pipeline import process_guard_requests
from services.observer.core import detect


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


def main():
    policy = fixture("policy.json")
    observations = fixture("observations.json")
    incident = detect(observations["failed-rollout"], policy)
    proposal = fixture("messages/proposal.json")
    proposal["incident_id"] = incident["incident_id"]
    dependencies = fixture("messages/dependency-result.json")
    dependencies["incident_id"] = incident["incident_id"]
    execution = fixture("messages/execution-result.json")
    execution["incident_id"] = incident["incident_id"]
    outcome = fixture("messages/outcome.json")
    outcome["incident_id"] = incident["incident_id"]
    state_history = []
    # This directory is removed after the store closes; no retained demo database.
    with tempfile.TemporaryDirectory() as directory:
        with SQLiteStore(Path(directory) / "coordinator.sqlite3", clock=lambda: datetime(
                2026, 10, 10, 12, 0, 36, tzinfo=timezone.utc)) as store:
            bus = LocalEventBus()

            def receive(message):
                handle(message, store, bus)
                state_history.append(store.incident(incident["incident_id"])["state"])

            def planner(event):
                receive(proposal)  # Mock planner; owner 3 is still a stub.

            def guard(event):
                second = 40 if store.approval(proposal["action_id"]) else 35
                process_guard_requests(
                    store, bus, state_reader=lambda p: observations["failed-rollout"][-1],
                    policy_reader=lambda p: policy, baseline_reader=lambda p: fixture("baseline.json"),
                    dependency_reader=lambda p: dependencies,
                    clock=lambda: datetime(2026, 10, 10, 12, 0, second, tzinfo=timezone.utc),
                )
                state_history.append(store.incident(incident["incident_id"])["state"])

            def executor(event):
                receive(execution)  # Synthetic result; makes no Kubernetes write.

            def verifier(event):
                receive(outcome)  # Synthetic health result; owner 6 is still a stub.

            def subscriber(callback):
                def consume(event):
                    callback(event)
                    acknowledge(store, event["event_id"])
                return consume

            for topic, callback in (("planner.request", planner), ("guard.request", guard),
                                    ("executor.request", executor), ("verifier.request", verifier),
                                    ("incident.outcome", lambda event: None)):
                bus.subscribe(topic, subscriber(callback))
            receive(incident)
            bus.drain()
            approve(proposal["action_id"], "demo-operator", store)
            state_history.append(store.incident(incident["incident_id"])["state"])
            dispatch_pending(store, bus)
            bus.drain()
            print(json.dumps({"mode": "offline; mock planner/executor/verifier",
                              "states": state_history, "incident": store.incident(incident["incident_id"])}, indent=2))


if __name__ == "__main__":
    main()
