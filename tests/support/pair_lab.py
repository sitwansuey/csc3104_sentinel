"""Two fake targets sharing one real Coordinator, Planner, Guard, Executor and Verifier (P4.05 tests).

Each target is a renamed copy of the sample demo-api fixtures with its own fake
Deployment, baseline and policy. The Guard, Executor and Verifier readers route
by the proposal's own target, as the live pair harness does. Readiness of the
services in the graph is set by the test (health), so a dependency can be made
BLOCKED and then CLEAR. Nothing here reads a cluster.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

from runtime.executor_pipeline import GuardAuthorizer, process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance
from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import LocalEventBus, SQLiteStore, approve, handle
from services.executor.core import ExecutionAdapters
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from services.guard.common import json_sha256
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore
from tests.support.executor_fake import FakeDeploymentAPI

ROOT = Path(__file__).resolve().parents[2]
# Which sample observation series gives each kind of fault.
FAULTS = {"config": "failed-rollout", "pod": "pod-failure"}


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


def renamed(value, name):
    """A copy of a demo-api fixture for the Deployment name (labels, UIDs and Pod names follow)."""
    text = json.dumps(value)
    for old, new in (("synthetic-demo-uid-001", name + "-uid"), ("synthetic-pod-uid", name + "-pod-uid"),
                     ("demo-api", name)):
        text = text.replace(old, new)
    return json.loads(text)


class Router:
    """One Deployment API facade over one fake Deployment per target name."""

    def __init__(self, clusters):
        self.clusters = clusters

    def read(self, target):
        return self.clusters[target["name"]].read(target)

    def conditional_restore(self, *, target, **arguments):
        return self.clusters[target["name"]].conditional_restore(target=target, **arguments)

    def fence(self, *, target, **arguments):
        return self.clusters[target["name"]].fence(target=target, **arguments)


class PairLab:
    """faults maps each target name to "config" (a bad rollout; ROLLBACK) or "pod" (failing Pods; RESTART).

    edges are (caller, callee) pairs of the reviewed graph; health holds each
    service's readiness for the dependency finding (the targets start unready).
    """

    def __init__(self, directory, faults, edges=()):
        self.path = Path(directory)
        self.now = datetime(2026, 10, 10, 12, 0, 35, tzinfo=UTC)
        self.bus = LocalEventBus()
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        self.ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.verification = VerificationStore(self.path / "verifier.db")
        self.faults = dict(faults)
        nodes = sorted(set(self.faults) | {node for edge in edges for node in edge})
        self.graph = {"edge_direction": "caller_to_callee", "nodes": nodes, "known_health": {},
                      "edges": [{"from": caller, "to": callee} for caller, callee in edges]}
        self.health = {node: node not in self.faults for node in nodes}
        self.clusters, self.baselines, self.policies, self.incidents = {}, {}, {}, {}
        for name, fault in self.faults.items():
            series = renamed(fixture("observations.json")[FAULTS[fault]], name)
            self.clusters[name] = FakeDeploymentAPI(series[-1], clock=lambda: self.now)
            baseline = renamed(fixture("baseline.json"), name)
            baseline["template_sha256"] = json_sha256(baseline["pod_template"])
            if isinstance(baseline.get("health_evidence"), dict):
                baseline["health_evidence"]["template_sha256"] = baseline["template_sha256"]
            self.baselines[name] = baseline
            policy = renamed(fixture("policy.json"), name)
            policy["allowed_actions"] = ["ROLLBACK", "RESTART"]
            policy["restart_constraints"] = {"targets": deepcopy(policy["targets"]), "max_restarts": 1,
                                             "window_seconds": 3600}
            self.policies[name] = policy
            self.incidents[name] = detect(series, policy)
        self.cluster = Router(self.clusters)
        self.authorizer = GuardAuthorizer(
            policy_reader=lambda proposal: self.policies[proposal["target"]["name"]],
            baseline_reader=lambda proposal: self.baselines[proposal["target"]["name"]],
            dependency_reader=self.dependency, approval_reader=lambda proposal: self.store.approval(proposal["action_id"]))
        self.adapters = ExecutionAdapters(ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / "locks"),
                                          cluster=self.cluster, authorizer=self.authorizer, clock=lambda: self.now)
        self.approvals = []

    def close(self):
        self.verification.close()
        self.ledger.close()
        self.store.close()

    def tick(self, seconds=1):
        self.now += timedelta(seconds=seconds)

    def open(self):
        """Deliver both incidents to the Coordinator; returns {name: incident_id}."""
        for incident in self.incidents.values():
            handle(incident, self.store, self.bus)
        return {name: incident["incident_id"] for name, incident in self.incidents.items()}

    def state(self, name):
        return self.store.incident(self.incidents[name]["incident_id"])["state"]

    def writes(self, name):
        return len(self.clusters[name].writes)

    # Routed readers.

    def live_state(self, proposal):
        state = self.cluster.read(proposal["target"])
        if proposal["action"] == "RESTART":
            state["restart_history"] = self.ledger.restart_history(proposal["target"])
        return state

    def dependency(self, proposal):
        state = dict(self.cluster.read(proposal["target"]), known_health=dict(self.health))
        return inspect_dependencies(proposal, self.graph, state, now=self.now)

    # One pass of each worker over every pending request.

    def plan(self):
        return process_planner_requests(
            self.store, self.bus, baseline_reader=lambda incident: [self.baselines[incident["target"]["name"]]],
            retriever=retrieve_runbooks, provider=mock_provider,
            template_reader=lambda incident: self.cluster.read(incident["target"])["deployment"]["spec"]["template"])

    def assess(self, proposal):
        self.tick()
        handle(self.dependency(proposal), self.store, self.bus)

    def guard(self):
        self.tick()
        return process_guard_requests(
            self.store, self.bus, state_reader=self.live_state,
            policy_reader=lambda proposal: self.policies[proposal["target"]["name"]],
            baseline_reader=lambda proposal: self.baselines[proposal["target"]["name"]],
            dependency_reader=self.dependency, clock=lambda: self.now)

    def approve(self, proposal, decision):
        self.tick()
        self.approvals.append(proposal["action_id"])
        return approve(proposal["action_id"], "offline-operator", self.store)

    def execute(self, proposal=None):
        self.tick()
        return process_executor_requests(self.store, self.bus, adapters=self.adapters)

    def repaired(self, name):
        """What the fake cluster shows after a repair: the target ready again, its Pods healthy."""
        snapshot = self.clusters[name].snapshot
        snapshot["deployment"]["status"].update(readyReplicas=1, availableReplicas=1)
        for pod in snapshot.get("pods", []):
            for status in pod.get("status", {}).get("containerStatuses", []):
                status["ready"] = True
        self.health[name] = True

    def readings(self, execution, *, healthy=True):
        at = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
        return [{"at": (at + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
                 "service": execution["target"]["name"], "http_status": 200 if healthy else 503,
                 "ready_replicas": 1 if healthy else 0, "desired_replicas": 1, "dependencies": {}}
                for offset in (1, 11, 21)]

    def verify(self, proposal=None, execution=None, *, healthy=None):
        """Verify every pending execution; healthy maps names to the readings' health (default True)."""
        healthy = healthy or {}
        self.now += timedelta(seconds=30)

        def sample_reader(current):
            name = current["target"]["name"]
            if healthy.get(name, True):
                self.repaired(name)
            return self.readings(current, healthy=healthy.get(name, True))

        return process_verifier_requests(self.store, self.bus, ledger=self.verification, sample_reader=sample_reader,
                                         policy_reader=lambda current: self.policies[current["target"]["name"]],
                                         clock=lambda: self.now)

    def advance(self, name, incident_id, *, approve=None, verify_health=None):
        """runtime.recovery_runner.advance for one incident, with this lab's routed workers."""
        return advance(self.store, incident_id, plan=self.plan, assess=self.assess, guard=self.guard,
                       approve=approve or self.approve, execute=self.execute,
                       verify=lambda proposal, execution: self.verify(healthy=verify_health))
