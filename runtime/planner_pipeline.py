"""Local durable Planner request handoff using injected offline dependencies."""
from copy import deepcopy
import json

from services.coordinator import acknowledge, dispatch_pending, record_message
from services.planner import propose


def process_planner_requests(store, bus, *, baseline_reader, retriever, provider):
    proposals = []
    pending = store.db.execute("SELECT id FROM outbox WHERE topic='planner.request' AND delivered=0 ORDER BY id").fetchall()
    for row in pending:
        store.db.execute('BEGIN IMMEDIATE')
        try:
            request = store.db.execute('SELECT * FROM outbox WHERE id=?', (row[0],)).fetchone()
            if request['delivered']:
                store.db.execute('COMMIT')
                continue
            incident = json.loads(request['payload'])['incident']
            current = store.incident(incident['incident_id'])
            if current and current['state'] == 'OPEN':
                proposal = propose(incident, baseline_reader(deepcopy(incident)), retriever, provider)
                record_message(proposal, store)
                proposals.append(proposal)
            acknowledge(store, row[0])
            store.db.execute('COMMIT')
        except BaseException:
            store.db.execute('ROLLBACK')
            raise
    dispatch_pending(store, bus)
    return proposals
