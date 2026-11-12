"""Six real components; explicitly fake Kubernetes, health readings and provider.

Planning goes through the durable Planner journal (PlannerStore) rather than the
legacy non-journalled path. Crash replay and bounded retries are proven by
tests/test_planner_pipeline.py; this demo shows the journalled happy path.

Two scenarios run: 'failed-rollout' (a bad template change, repaired by
ROLLBACK) and 'pod-failure' (Pods not ready while the template still equals the
reviewed baseline, repaired by RESTART under a restart-enabled policy).
"""
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
from services.planner.store import PlannerStore
from services.verifier.core import inspect_dependencies
from services.verifier.store import VerificationStore
from tests.support.executor_fake import FakeDeploymentAPI


def fixture(name):
    return json.loads((ROOT / 'data/sentinel/samples' / name).read_text(encoding='utf-8'))


def restart_policy(policy, *, max_restarts=1, window_seconds=3600):
    """The sample policy with RESTART allowed for its target, under a restart budget."""
    policy = deepcopy(policy)
    policy['allowed_actions'] = [*policy['allowed_actions'], 'RESTART']
    policy['restart_constraints'] = {'targets': deepcopy(policy['targets']),
                                     'max_restarts': max_restarts, 'window_seconds': window_seconds}
    return policy


class OfflineFlow:
    def __init__(self, directory, scenario='failed-rollout'):
        self.path = Path(directory)
        self.scenario = scenario
        self.now = datetime(2026, 10, 10, 12, 0, 35, tzinfo=timezone.utc)
        self.bus = LocalEventBus()
        self.store = SQLiteStore(self.path / 'coordinator.db', clock=lambda: self.now)
        self.ledger = SQLiteExecutionLedger(self.path / 'executor.db')
        self.verification = VerificationStore(self.path / 'verifier.db')
        self.policy, self.baseline, self.graph = fixture('policy.json'), fixture('baseline.json'), fixture('dependencies.json')
        if scenario == 'pod-failure':
            self.policy = restart_policy(self.policy)
        self.cluster = FakeDeploymentAPI(fixture('observations.json')[scenario][-1], clock=lambda: self.now)
        self.authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
            dependency_reader=lambda p: inspect_dependencies(p, self.graph, self.cluster.read(p['target']), now=self.now),
            approval_reader=lambda p: self.store.approval(p['action_id']))
        self.adapters = ExecutionAdapters(ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / 'locks'),
            cluster=self.cluster, authorizer=self.authorizer, clock=lambda: self.now)
        # Opened on first use so callers that bring their own journal are unaffected.
        self.planner = None
        self.provider_calls = 0

    def provider(self, context):
        self.provider_calls += 1
        return mock_provider(context)

    def plan(self):
        if self.planner is None:
            self.planner = PlannerStore(self.path / 'planner-journal.db', clock=lambda: self.now)
        return process_planner_requests(self.store, self.bus, baseline_reader=lambda incident: [self.baseline],
            retriever=retrieve_runbooks, provider=self.provider, journal=self.planner,
            template_reader=lambda incident: self.cluster.read(incident['target'])['deployment']['spec']['template'])

    def close(self):
        if self.planner is not None:
            self.planner.close()
        self.verification.close()
        self.ledger.close()
        self.store.close()

    def restart(self):
        # Restart Coordinator's consumer with an empty in-memory bus. Durable
        # handoffs and Executor journal remain authoritative; fake API stays up.
        self.store.close()
        self.bus = LocalEventBus()
        self.store = SQLiteStore(self.path / 'coordinator.db', clock=lambda: self.now)
        if self.planner is not None:
            self.planner.close()
            self.planner = PlannerStore(self.path / 'planner-journal.db', clock=lambda: self.now)

    def live_state(self, proposal):
        """The Guard's trusted read; a RESTART also gets the Executor ledger's restart history."""
        state = self.cluster.read(proposal['target'])
        if proposal['action'] == 'RESTART':
            state['restart_history'] = self.ledger.restart_history(proposal['target'])
        return state

    def prepare(self):
        self.incident = detect(fixture('observations.json')[self.scenario], self.policy)
        handle(self.incident, self.store, self.bus)
        self.proposal = self.plan()[0]
        handle(inspect_dependencies(self.proposal, self.graph, self.cluster.read(self.proposal['target']), now=self.now), self.store, self.bus)

    def guard(self):
        return process_guard_requests(self.store, self.bus, state_reader=self.live_state,
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


def demonstrate_restart():
    """RESTART end to end: Pods fail while the template equals the reviewed baseline."""
    with tempfile.TemporaryDirectory() as directory:
        flow = OfflineFlow(directory, scenario='pod-failure')
        try:
            flow.prepare()
            assert flow.proposal['action'] == 'RESTART', flow.proposal
            flow.authorize()
            assert flow.state() == 'READY', flow.state()
            flow.execute()
            assert flow.state() == 'VERIFYING'
            written = flow.cluster.writes[-1]['deployment']
            flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=timezone.utc)
            flow.verify(fixture('health.json')['stable'])
            assert flow.state() == 'RECOVERED'
            assert len(flow.cluster.writes) == 1
            token = written['spec']['template']['metadata']['annotations']['sentinel.io/restart-token']
            assert token == flow.proposal['parameters']['restart_token']
            assert json.loads(written['metadata']['annotations']['sentinel.io/execution'])['action'] == 'RESTART'
            print(json.dumps({'mode': 'offline_fake_infrastructure_mock_provider', 'scenario': 'pod-failure',
                'action': flow.proposal['action'], 'status': flow.state(), 'writes': len(flow.cluster.writes),
                'restart_history': len(flow.ledger.restart_history(flow.proposal['target']))}))
        finally:
            flow.close()


def demonstrate():
    with tempfile.TemporaryDirectory() as directory:
        flow = OfflineFlow(directory)
        try:
            flow.prepare()
            flow.authorize()
            flow.restart()
            # After a restart the delivered request is not planned again.
            assert flow.plan() == []
            assert flow.provider_calls == 1
            saved = flow.planner.get(flow.incident['incident_id'])
            assert saved['proposal'] == flow.proposal and saved['delivered'] and saved['attempts'] == 1
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
                'planner': 'durable journal', 'provider_calls': flow.provider_calls,
                'status': flow.state(), 'writes': len(flow.cluster.writes), 'restart': 'passed'}))
        finally:
            flow.close()


if __name__ == '__main__':
    demonstrate()
    demonstrate_restart()
