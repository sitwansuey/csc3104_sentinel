"""Opt-in synthetic remote delivery smoke check; never a recovery acceptance."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import ssl
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from runtime.worker_http import WorkerClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--ca', required=True)
    args = parser.parse_args()
    tls = ssl.create_default_context(cafile=args.ca)
    observer = WorkerClient(args.url, 'observer-1', os.environ['SENTINEL_OBSERVER_TOKEN'], tls_context=tls)
    planner = WorkerClient(args.url, 'planner-1', os.environ['SENTINEL_PLANNER_TOKEN'], tls_context=tls)
    root = Path(__file__).resolve().parents[2]
    incident = json.loads((root / 'data/sentinel/samples/messages/incident.json').read_text())
    proposal = json.loads((root / 'data/sentinel/samples/messages/proposal.json').read_text())
    suffix = uuid4().hex
    incident['incident_id'] = 'synthetic-transport-' + suffix
    incident['target']['uid'] = 'synthetic-transport-' + suffix
    proposal.update(incident_id=incident['incident_id'], action_id='synthetic-action-' + suffix,
                    target=deepcopy(incident['target']))
    observer.post('ingest', {'message': incident})
    delivery = planner.post('claim', {})['delivery']
    if not delivery or delivery['payload'].get('incident') != incident:
        raise RuntimeError('Use an isolated empty Coordinator database for this synthetic smoke check')
    body = {'delivery_id': delivery['delivery_id'], 'lease_token': delivery['lease_token'], 'result': proposal}
    first = planner.post('result', body)
    replay = planner.post('result', body)
    if first != {'accepted': True, 'replay': False} or replay != {'accepted': True, 'replay': True}:
        raise RuntimeError('Unexpected result acknowledgment or replay receipt')
    print(json.dumps({'mode': 'synthetic_transport_only', 'incident_id': incident['incident_id'],
                      'delivery_id': delivery['delivery_id'], 'result': first, 'replay': replay}))


if __name__ == '__main__':
    main()
