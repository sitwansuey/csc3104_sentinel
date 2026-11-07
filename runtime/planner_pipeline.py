"""Local durable Planner request handoff using injected offline dependencies."""
from copy import deepcopy
import json

from services.coordinator.core import acknowledge, dispatch_pending, record_message
from services.planner.core import propose


def process_planner_requests(store, bus, *, baseline_reader, retriever, provider):
    proposals = []
    pending = store.pending_routes("planner.request")
    for row in pending:
        with store.transaction():
            request = store.route(row[0])
            if request['delivered']:
                continue
            incident = json.loads(request['payload'])['incident']
            current = store.incident(incident['incident_id'])
            if current and current['state'] == 'OPEN':
                proposal = propose(incident, baseline_reader(deepcopy(incident)), retriever, provider)
                record_message(proposal, store)
                proposals.append(proposal)
            acknowledge(store, row[0])
    dispatch_pending(store, bus)
    return proposals
