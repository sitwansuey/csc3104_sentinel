from copy import deepcopy
import json
from pathlib import Path
import unittest

from services.planner.core import propose, retrieve_runbooks, mock_provider
from shared.contracts import ContractError, validate_message

ROOT = Path(__file__).parents[1]

class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.incident = json.loads((ROOT / 'data/sentinel/samples/messages/incident.json').read_text())
        self.baseline = json.loads((ROOT / 'data/sentinel/samples/baseline.json').read_text())

    def propose(self, baselines=None, provider=mock_provider):
        return propose(self.incident, [self.baseline] if baselines is None else baselines,
                       retrieve_runbooks, provider)

    def test_retrieves_runbook_and_returns_exact_saved_baseline(self):
        self.assertEqual(retrieve_runbooks(self.incident)[0]['id'], 'failed-rollout')
        result = self.propose()
        validate_message(result)
        self.assertEqual(result['parameters'], {key: self.baseline[key] for key in ('baseline_id', 'template_sha256')})
        self.assertEqual(result, self.propose())

    def test_absent_wrong_target_future_or_tampered_baseline_escalates(self):
        for change in ({'target': {}}, {'healthy_observed_at': '2099-01-01T00:00:00Z'},
                       {'template_sha256': 'f' * 64}, {'healthy_observed_at': 'invalid'}):
            with self.subTest(change=change):
                self.assertEqual(self.propose([dict(self.baseline, **change)])['action'], 'ESCALATE')
        self.assertEqual(self.propose([])['action'], 'ESCALATE')

    def test_provider_cannot_change_identity_select_another_template_or_invent_evidence(self):
        for change in ({'target': {}}, {'parameters': {'baseline_id': 'invented', 'template_sha256': 'a' * 64}},
                       {'evidence_refs': ['invented']}, {'action': 'SCALE', 'parameters': {'desired_replicas': 5}}):
            with self.subTest(change=change), self.assertRaises(ContractError):
                self.propose(provider=lambda context: {**mock_provider(context), **change})

    def test_missing_runbook_escalates_without_calling_provider(self):
        result = propose(self.incident, [self.baseline], lambda incident: [],
                         lambda context: self.fail('Provider should not be called'))
        self.assertEqual(result['action'], 'ESCALATE')

    def test_provider_input_mutation_cannot_change_trusted_baseline(self):
        before = deepcopy(self.baseline)
        def provider(context):
            context['baselines'][0]['template_sha256'] = 'a' * 64
            return mock_provider(context)
        with self.assertRaises(ContractError):
            self.propose(provider=provider)
        self.assertEqual(self.baseline, before)


def metric_signal(evidence_id='E-METRIC-1', name='k8s.pod.cpu.usage'):
    return {'evidence_id': evidence_id, 'source': 'otel_metric_alert', 'reason': name,
            'detail': '{"above":0.18,"metric":"' + name + '","unit":"cores","values":[0.19]}',
            'at': '2026-10-10T12:00:30Z'}


class MetricOnlyTests(unittest.TestCase):
    """A metric alert with every Pod ready is escalated by a code rule; no provider can turn it into a repair."""

    def setUp(self):
        self.incident = json.loads((ROOT / 'data/sentinel/samples/messages/incident.json').read_text())
        self.baseline = json.loads((ROOT / 'data/sentinel/samples/baseline.json').read_text())
        warning = {'evidence_id': 'E-EVENT-1', 'source': 'event', 'reason': 'Unhealthy',
                   'detail': 'Readiness probe took 900 ms', 'at': '2026-10-10T12:00:30Z'}
        self.metric_only = dict(self.incident, signals=[metric_signal(), warning])

    def test_metric_only_incident_escalates_without_calling_any_provider(self):
        from services.planner.core import METRIC_ONLY_RATIONALE, metric_only
        self.assertTrue(metric_only(self.metric_only))
        for action in ('ROLLBACK', 'RESTART'):
            def provider(context, action=action):
                self.fail('A metric-only incident must not reach a provider asking for ' + action)
            with self.subTest(action=action):
                result = propose(self.metric_only, [self.baseline], retrieve_runbooks, provider,
                                 live_template=deepcopy(self.baseline['pod_template']))
                validate_message(result)
                self.assertEqual((result['action'], result['parameters'], result['rationale']),
                                 ('ESCALATE', {}, METRIC_ONLY_RATIONALE))
                self.assertEqual(result['evidence_refs'], ['E-METRIC-1', 'E-EVENT-1'])

    def test_metric_only_escalates_in_agent_mode_too(self):
        diagnosis = {'incident_id': self.metric_only['incident_id'],
                     'prediction': {'component': 'demo-api', 'reason': 'resource saturation'}}
        result = propose(self.metric_only, [self.baseline], lambda query: self.fail('no retrieval'),
                         lambda context: {'action': 'ROLLBACK', 'parameters': {}, 'rationale': 'model'},
                         diagnosis=diagnosis)
        self.assertEqual(result['action'], 'ESCALATE')

    def test_a_metric_alert_with_pod_failure_still_reaches_the_provider(self):
        from services.planner.core import metric_only
        incident = dict(self.incident, signals=[*self.incident['signals'], metric_signal()])
        self.assertFalse(metric_only(incident))
        self.assertFalse(metric_only(self.incident))
        result = propose(incident, [self.baseline], retrieve_runbooks, mock_provider)
        self.assertEqual(result['action'], 'ROLLBACK')
        # Replica status alone also counts as workload failure.
        status = {'evidence_id': 'E-DEPLOY-1', 'source': 'deployment_status', 'reason': 'UnavailableReplicas',
                  'detail': 'desired 1, ready 0', 'at': '2026-10-10T12:00:30Z'}
        self.assertFalse(metric_only(dict(self.incident, signals=[metric_signal(), status])))

    def test_without_the_rule_the_provider_would_have_proposed_a_rollback(self):
        # The control: the same evidence minus the metric alert is not escalated by the rule.
        from services.planner.core import metric_only
        warning_only = dict(self.metric_only, signals=self.metric_only['signals'][1:])
        self.assertFalse(metric_only(warning_only))
        result = propose(warning_only, [self.baseline], retrieve_runbooks, mock_provider)
        self.assertEqual(result['action'], 'ROLLBACK')

    def test_metric_alerts_retrieve_the_resource_saturation_runbook(self):
        ids = [item['id'] for item in retrieve_runbooks(dict(self.incident, signals=[*self.incident['signals'],
                                                                                      metric_signal()]))]
        self.assertEqual(ids, ['failed-rollout', 'resource-saturation'])
        self.assertEqual([item['id'] for item in retrieve_runbooks(self.incident)], ['failed-rollout'])
        content = retrieve_runbooks(self.metric_only)[-1]['content']
        self.assertIn('Do not roll back or restart on a usage alert alone', content)
