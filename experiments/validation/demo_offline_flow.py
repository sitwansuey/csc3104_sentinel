"""Six real components; explicitly fake Kubernetes, health readings and provider."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime.planner_pipeline import process_planner_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.executor_pipeline import GuardAuthorizer, process_executor_requests
from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import SQLiteStore, LocalEventBus, approve, handle
from services.executor.core import ExecutionAdapters
from services.executor.store import SQLiteExecutionLedger, LocalTargetCoordinator
from services.observer.core import detect
from services.planner.core import retrieve_runbooks, mock_provider
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore
from tests.support.executor_fake import FakeDeploymentAPI


def fixture(name):
    return json.loads((ROOT / 'data/sentinel/samples' / name).read_text(encoding='utf-8'))


class OfflineFlow:
    def __init__(self, directory):
        self.path = Path(directory)
        self.now = datetime(2026, 10, 10, 12, 0, 35, tzinfo=timezone.utc)
        self.bus = LocalEventBus()
        self.store = SQLiteStore(self.path / 'coordinator.db', clock=lambda: self.now)
        self.ledger = SQLiteExecutionLedger(self.path / 'executor.db')
        self.verification = VerificationStore(self.path / 'verifier.db')
        self.policy, self.baseline, self.graph = fixture('policy.json'), fixture('baseline.json'), fixture('dependencies.json')
        self.cluster = FakeDeploymentAPI(fixture('observations.json')['failed-rollout'][-1], clock=lambda: self.now)
        self.authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: inspect_dependencies(p, self.graph, self.cluster.read(p['target']), now=self.now),
            approval_reader=lambda p: self.store.approval(p['action_id']))
        self.adapters = ExecutionAdapters(ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / 'locks'),
            cluster=self.cluster, authorizer=self.authorizer, clock=lambda: self.now)

    def close(self):
        self.verification.close()
        self.ledger.close()
        self.store.close()

    def restart(self):
        # Restart Coordinator's consumer with an empty in-memory bus. Durable
        # handoffs and Executor journal remain authoritative; fake API stays up.
        self.store.close()
        self.bus = LocalEventBus()
        self.store = SQLiteStore(self.path / 'coordinator.db', clock=lambda: self.now)

    def prepare(self):
        self.incident = detect(fixture('observations.json')['failed-rollout'], self.policy)
        handle(self.incident, self.store, self.bus)
        self.proposal = process_planner_requests(self.store, self.bus,
            baseline_reader=lambda incident: [self.baseline], retriever=retrieve_runbooks, provider=mock_provider)[0]
        handle(inspect_dependencies(self.proposal, self.graph, self.cluster.read(self.proposal['target']), now=self.now), self.store, self.bus)

    def guard(self):
        return process_guard_requests(self.store, self.bus, state_reader=lambda p: self.cluster.read(p['target']),
            policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: inspect_dependencies(p, self.graph, self.cluster.read(p['target']), now=self.now),
            clock=lambda: self.now)

    def authorize(self):
        self.guard()
        if self.state() == 'AWAITING_APPROVAL':
            self.now = self.now.replace(second=36)
            approve(self.proposal['action_id'], 'offline-operator', self.store)
            self.now = self.now.replace(second=40)
            self.guard()

    def execute(self):
        return process_executor_requests(self.store, self.bus, adapters=self.adapters)

    def verify(self, readings):
        return process_verifier_requests(self.store, self.bus, ledger=self.verification,
            sample_reader=lambda execution: deepcopy(readings), policy_reader=lambda execution: self.policy,
            clock=lambda: self.now)

    def state(self):
        return self.store.incident(self.incident['incident_id'])['state']


def demonstrate():
    with tempfile.TemporaryDirectory() as directory:
        flow = OfflineFlow(directory)
        try:
            flow.prepare()
            flow.authorize()
            flow.restart()
            flow.execute()
            assert flow.state() == 'VERIFYING'
            flow.now = datetime(2026, 10, 10, 12, 0, 50, tzinfo=timezone.utc)
            assert flow.verify(fixture('health.json')['stable'][:1]) == []
            assert flow.state() == 'VERIFYING'
            flow.restart()
            flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=timezone.utc)
            flow.verify(fixture('health.json')['stable'])
            assert flow.state() == 'RECOVERED'
            assert len(flow.cluster.writes) == 1
            print(json.dumps({'mode': 'offline_fake_infrastructure_mock_provider',
                'components': ['Observer', 'Planner', 'Coordinator', 'Guard', 'Executor', 'Verifier'],
                'status': flow.state(), 'writes': len(flow.cluster.writes), 'restart': 'passed'}))
        finally:
            flow.close()


if __name__ == '__main__':
    demonstrate()
