"""Reproduce real dependency assessment and queued-authorization revocation offline."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.guard_pipeline import process_guard_requests
from services.coordinator import LocalEventBus, SQLiteStore, approve, handle
from services.verifier import inspect_dependencies


def fixture(name):
    return json.loads((ROOT / "samples" / name).read_text(encoding="utf-8"))


def at(second):
    return datetime(2026, 10, 10, 12, 0, second, tzinfo=timezone.utc)


def scenario(health, expected):
    proposal, incident = fixture("messages/proposal.json"), fixture("messages/incident.json")
    state, policy = fixture("observations.json")["failed-rollout"][-1], fixture("policy.json")
    baseline, graph = fixture("baseline.json"), fixture("dependencies.json")
    action_id = proposal["action_id"]
    with SQLiteStore(clock=lambda: at(36)) as store:
        bus = LocalEventBus()

        def process(second):
            return process_guard_requests(
                store, bus, state_reader=lambda p: deepcopy(state),
                policy_reader=lambda p: deepcopy(policy), baseline_reader=lambda p: deepcopy(baseline),
                clock=lambda: at(second))

        def pending_executors():
            return store.db.execute(
                "SELECT count(*) FROM outbox WHERE topic='executor.request' AND delivered=0").fetchone()[0]

        for message in (incident, proposal, inspect_dependencies(proposal, graph, state, now=at(34))):
            handle(message, store, bus)
        assert process(35)[0]["reason_codes"] == ["OPERATOR_APPROVAL_REQUIRED"]
        approve(action_id, "offline-demo-operator", store)
        assert process(40)[0]["verdict"] == "ALLOW"
        assert store.action(action_id)["state"] == "READY" and pending_executors() == 1

        graph["known_health"]["mock-db"] = health
        finding = inspect_dependencies(proposal, graph, state, now=at(41))
        handle(finding, store, bus)
        after_ingest = store.action(action_id)["state"]
        assert after_ingest == "VALIDATING" and pending_executors() == 0
        assert not any(event["topic"] == "executor.request" for event in bus.events)
        decision = process(42)[0]
        assert decision["verdict"] == expected
        assert pending_executors() == int(expected == "ALLOW")
        return {"dependency_status": finding["status"], "before_update": "READY",
                "after_ingest": after_ingest, "pending_execution_after_ingest": 0,
                "guard_verdict": decision["verdict"], "reason_codes": decision["reason_codes"],
                "final_state": store.action(action_id)["state"],
                "pending_execution_final": pending_executors()}


def main():
    print(json.dumps({"source": "synthetic offline fixtures; no Kubernetes writes",
                      "scenarios": [scenario(True, "ALLOW"), scenario(False, "REJECT"),
                                    scenario(None, "ESCALATE")]}, indent=2))


if __name__ == "__main__":
    main()
