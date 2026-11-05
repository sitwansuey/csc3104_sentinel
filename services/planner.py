"""Offline runbook retrieval and bounded, validated provider proposals."""
from copy import deepcopy
from pathlib import Path

from services.guard_common import json_sha256, utc_timestamp
from shared.contracts import ContractError, validate_message


def retrieve_runbooks(incident):
    """Retrieve trusted local failed-rollout guidance; no external model call."""
    reasons = ' '.join(signal['reason'] for signal in incident['signals']).lower()
    if not any(word in reasons for word in ('crash', 'startup', 'rollout', 'backoff', 'unavailable')):
        return []
    path = Path(__file__).resolve().parents[1] / 'runbooks/failed-rollout.md'
    return [{'id': 'failed-rollout', 'content': path.read_text(encoding='utf-8')}]


def mock_provider(context):
    """Deterministic offline provider; does not claim LLM or RCA capability."""
    baseline = context['baselines'][0]
    return {'action': 'ROLLBACK',
            'parameters': {key: baseline[key] for key in ('baseline_id', 'template_sha256')},
            'rationale': 'Restore the saved observed healthy template described by the failed-rollout runbook.',
            'evidence_refs': [item['evidence_id'] for item in context['incident']['signals']]}


def propose(incident: dict, baselines: list[dict], retriever, provider) -> dict:
    """Return a typed action with incident evidence; escalate if no baseline exists."""
    validate_message(incident)
    if incident['kind'] != 'incident' or not isinstance(baselines, list):
        raise ContractError('Planner requires an incident and a baseline list')
    incident = deepcopy(incident)
    at = utc_timestamp(incident['observed_at'], 'observed_at')
    suitable = []
    for item in baselines:
        if not isinstance(item, dict) or item.get('target') != incident['target']:
            continue
        try:
            valid = (isinstance(item.get('baseline_id'), str) and bool(item['baseline_id'].strip())
                     and isinstance(item.get('pod_template'), dict) and bool(item['pod_template'])
                     and item.get('template_sha256') == json_sha256(item['pod_template'])
                     and utc_timestamp(item.get('healthy_observed_at'), 'baseline time') <= at)
        except ValueError:
            valid = False
        if valid:
            suitable.append(deepcopy(item))
    suitable.sort(key=lambda item: utc_timestamp(item['healthy_observed_at'], 'baseline time'), reverse=True)
    runbooks = retriever(deepcopy(incident))
    if not isinstance(runbooks, list) or len(runbooks) > 10 or any(
            not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or not isinstance(item.get('content'), str) or not 1 <= len(item['content']) <= 20000
            for item in runbooks):
        raise ContractError('Retriever must return bounded identified runbooks')
    if not suitable or not runbooks:
        answer = {'action': 'ESCALATE', 'parameters': {},
                  'rationale': 'No suitable saved healthy baseline or relevant runbook is available.',
                  'evidence_refs': [item['evidence_id'] for item in incident['signals']]}
    else:
        answer = provider(deepcopy({'incident': incident, 'baselines': suitable, 'runbooks': runbooks}))
    if not isinstance(answer, dict) or set(answer) != {'action', 'parameters', 'rationale', 'evidence_refs'}:
        raise ContractError('Provider response must contain only typed proposal fields')
    message = {'schema_version': '1.0', 'kind': 'proposal',
               'incident_id': incident['incident_id'], 'target': incident['target'],
               'based_on_resource_version': incident['observed_resource_version'], **deepcopy(answer)}
    message['action_id'] = 'ACT-' + json_sha256(message)[:24]
    validate_message(message)
    if message['action'] not in {'ROLLBACK', 'ESCALATE'}:
        raise ContractError('Offline Planner supports only saved-template rollback or escalation')
    if message['action'] == 'ROLLBACK' and not any(
            message['parameters'] == {key: item[key] for key in ('baseline_id', 'template_sha256')}
            for item in suitable):
        raise ContractError('Provider selected an unavailable or changed healthy baseline')
    if not set(message['evidence_refs']) <= {item['evidence_id'] for item in incident['signals']}:
        raise ContractError('Provider invented an evidence reference')
    return message
