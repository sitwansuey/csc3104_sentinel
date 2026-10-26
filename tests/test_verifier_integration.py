from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest

from services.coordinator import LocalEventBus, SQLiteStore, approve, handle
from services.guard import evaluate
from services.verifier import inspect_dependencies, verify

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def load(name):
    return json.loads((ROOT / "samples" / name).read_text(encoding="utf-8"))


class VerifierIntegrationTests(unittest.TestCase):
    def test_real_verifier_outcomes_drive_coordinator_lifecycle(self):
        health = load("health.json")
        cases = [(health["stable"], "RECOVERED"),
                 (health["unstable"], "FAILED"),
                 (health["stable"][:1], "NEEDS_REVIEW")]
        for samples, expected in cases:
            with self.subTest(expected=expected):
                store = SQLiteStore(clock=lambda: NOW)
                try:
                    bus = LocalEventBus()
                    incident = load("messages/incident.json")
                    proposal = load("messages/proposal.json")
                    state = load("observations.json")["failed-rollout"][-1]
                    policy, baseline = load("policy.json"), load("baseline.json")
                    dependencies = inspect_dependencies(proposal, load("dependencies.json"), state, now=NOW)
                    for message in (incident, proposal, dependencies):
                        handle(message, store, bus)
                    handle(evaluate(proposal, state, policy, baseline, dependencies, now=NOW), store, bus)
                    approval = approve(proposal["action_id"], "offline-operator", store)
                    handle(evaluate(proposal, state, policy, baseline, dependencies, approval,
                                    now=NOW + timedelta(seconds=1)), store, bus)
                    execution = load("messages/execution-result.json")
                    handle(execution, store, bus)
                    self.assertEqual(store.incident(incident["incident_id"])["state"], "VERIFYING")
                    handle(verify(execution, deepcopy(samples), policy), store, bus)
                    self.assertEqual(store.incident(incident["incident_id"])["state"], expected)
                finally:
                    store.close()

    def test_guard_rejects_real_dependency_blocker(self):
        proposal = load("messages/proposal.json")
        state = load("observations.json")["failed-rollout"][-1]
        graph = load("dependencies.json")
        graph["known_health"]["mock-db"] = False
        dependencies = inspect_dependencies(proposal, graph, state, now=NOW)
        decision = evaluate(proposal, state, load("policy.json"), load("baseline.json"), dependencies, now=NOW)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("DEPENDENCY_BLOCKED", decision["reason_codes"])
