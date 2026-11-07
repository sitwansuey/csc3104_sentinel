from copy import deepcopy
import json
from pathlib import Path
import unittest

from services.planner.core import propose, retrieve_runbooks, mock_provider
from shared.contracts import ContractError, validate_message

ROOT = Path(__file__).parents[2]

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
