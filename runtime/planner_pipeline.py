"""Local durable Planner request handoff using injected offline dependencies."""
from copy import deepcopy
import json

from services.coordinator.core import acknowledge, dispatch_pending, record_message
from services.planner.core import propose
from services.planner.worker import prepare


def process_planner_requests(store, bus, *, baseline_reader, retriever, provider,
                             journal=None, max_attempts=3, retry_seconds=5, lease_seconds=120,
                             diagnoser=None, finalize=None, template_reader=None):
    """Plan pending incidents. diagnoser and finalize need the durable journal (see prepare()).

    template_reader(incident), when given, is a trusted read of the target's
    current Pod template; without it the Planner never proposes RESTART.
    """
    if journal is not None:
        return _process_durable(store, bus, baseline_reader=baseline_reader, retriever=retriever,
            provider=provider, journal=journal, max_attempts=max_attempts,
            retry_seconds=retry_seconds, lease_seconds=lease_seconds,
            diagnoser=diagnoser, finalize=finalize, template_reader=template_reader)
    if diagnoser is not None or finalize is not None:
        raise ValueError("Agent planning hooks require the durable Planner journal")
    proposals = []
    pending = store.pending_routes("planner.request")
    for row in pending:
        request = store.route(row[0])
        if request['delivered']:
            continue
        incident = json.loads(request['payload'])['incident']
        current = store.incident(incident['incident_id'])
        if current and current['state'] == 'OPEN' and not store.planning_blocker(incident['incident_id']):
            # Inference and external reads can take minutes. Never hold the
            # Coordinator writer while waiting for them.
            live_template = template_reader(deepcopy(incident)) if template_reader is not None else None
            proposal = propose(incident, baseline_reader(deepcopy(incident)), retriever, provider,
                               live_template=live_template)
        else:
            proposal = None
        with store.transaction():
            request = store.route(row[0])
            if request['delivered']:
                continue
            current = store.incident(incident['incident_id'])
            if current and current['state'] == 'OPEN':
                if proposal is not None and not store.planning_blocker(incident['incident_id']):
                    record_message(proposal, store)
                    proposals.append(proposal)
                else:
                    continue
            acknowledge(store, row[0])
    dispatch_pending(store, bus)
    return proposals


def _process_durable(store, bus, *, baseline_reader, retriever, provider, journal,
                     max_attempts, retry_seconds, lease_seconds, diagnoser, finalize, template_reader):
    proposals = []
    # A crash after Coordinator commit can precede the local delivery marker.
    # Reconcile from the exact accepted business payload, not a transport flag.
    for saved in journal.undelivered():
        action = store.action(saved["proposal"]["action_id"])
        if action and action["proposal"] == saved["proposal"]:
            journal.delivered(saved["incident_id"])
    for row in store.pending_routes("planner.request"):
        incident = json.loads(row["payload"])["incident"]
        current = store.incident(incident["incident_id"])
        if not current or current["state"] != "OPEN":
            with store.transaction():
                acknowledge(store, row["id"])
            continue
        if store.planning_blocker(incident["incident_id"]):
            continue
        proposal = prepare(incident, journal=journal, baseline_reader=baseline_reader,
            retriever=retriever, provider=provider, max_attempts=max_attempts,
            retry_seconds=retry_seconds, lease_seconds=lease_seconds,
            diagnoser=diagnoser, finalize=finalize, template_reader=template_reader)
        if proposal is None:
            continue
        with store.transaction():
            request = store.route(row["id"])
            current = store.incident(incident["incident_id"])
            if not request["delivered"] and current and current["state"] == "OPEN":
                if store.planning_blocker(incident["incident_id"]):
                    continue
                record_message(proposal, store)
                proposals.append(proposal)
            acknowledge(store, row["id"])
        journal.delivered(incident["incident_id"])
    dispatch_pending(store, bus)
    return proposals
