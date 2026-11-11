import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.dashboard import snapshot
from services.coordinator.core import SQLiteStore, LocalEventBus, _event_key, handle
from services.coordinator.read import read_records

SAMPLES = Path(__file__).parents[1] / 'data/sentinel/samples/messages'


def sample(name):
    return json.loads((SAMPLES / name).read_text())


class CoordinatorReadTests(unittest.TestCase):
    def test_public_reader_matches_persisted_incident_without_modifying_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'coordinator.db'
            incident = json.loads((Path(__file__).parents[1] / 'data/sentinel/samples/messages/incident.json').read_text())
            with SQLiteStore(path) as store:
                handle(incident, store, LocalEventBus())
            before = path.read_bytes()
            records = read_records(path, limit=1)
            self.assertEqual(records['incidents'][0]['incident_id'], incident['incident_id'])
            self.assertEqual(records['incidents'][0]['target'], incident['target'])
            self.assertEqual(records['messages'][0]['message'], incident)
            self.assertEqual(path.read_bytes(), before)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'missing.db'
            import sqlite3
            with self.assertRaises(sqlite3.Error):
                read_records(path)
            self.assertFalse(path.exists())

    def test_dashboard_uses_public_interface_and_keeps_unavailable_explicit(self):
        with patch('runtime.dashboard.read_records', side_effect=OSError('unavailable')) as reader:
            result = snapshot('coordinator.db', 'missing-telemetry.db')
        reader.assert_called_once_with('coordinator.db', limit=600)
        self.assertFalse(result['sources']['coordinator']['connected'])
        self.assertEqual(result['incidents'], [])

    def test_transitions_commit_with_state_and_are_read_newest_first_within_the_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'coordinator.db'
            incident, proposal = sample('incident.json'), sample('proposal.json')
            with SQLiteStore(path) as store:
                handle(incident, store, LocalEventBus())
                handle(proposal, store, LocalEventBus())
                state = store.incident(incident['incident_id'])['state']
            records = read_records(path, limit=600)
            moves = [(t['from_state'], t['to_state'], t['cause'], t['outbox_high_water'], t['action_id'])
                     for t in reversed(records['transitions'])]
            self.assertEqual(moves, [
                (None, 'OPEN', _event_key(incident), None, None),
                ('OPEN', 'VALIDATING', _event_key(proposal), 1, proposal['action_id'])])
            self.assertEqual(state, 'VALIDATING')
            self.assertEqual([t['seq'] for t in read_records(path, limit=1)['transitions']], [2])
            self.assertTrue(all(t['recorded_at'].endswith('Z') for t in records['transitions']))

    def test_a_failed_message_leaves_neither_state_nor_transition(self):
        with SQLiteStore() as store:
            handle(sample('incident.json'), store, LocalEventBus())
            with patch.object(store, 'enqueue', side_effect=RuntimeError('injected after activation')):
                with self.assertRaises(RuntimeError):
                    handle(sample('proposal.json'), store, LocalEventBus())
            self.assertEqual(store.incident(sample('incident.json')['incident_id'])['state'], 'OPEN')
            self.assertIsNone(store.action(sample('proposal.json')['action_id']))
            self.assertEqual([t['to_state'] for t in store.transitions()], ['OPEN'])

    def test_replayed_messages_write_no_transition(self):
        with SQLiteStore() as store:
            for name in ('incident.json', 'proposal.json'):
                self.assertTrue(handle(sample(name), store, LocalEventBus()))
            before = store.transitions()
            for name in ('incident.json', 'proposal.json'):
                self.assertFalse(handle(sample(name), store, LocalEventBus()))
            self.assertEqual(store.transitions(), before)
            self.assertEqual(len(before), 2)

    def test_transitions_are_append_only_per_change(self):
        with SQLiteStore() as store:
            handle(sample('incident.json'), store, LocalEventBus())
            handle(sample('proposal.json'), store, LocalEventBus())
            incident_id, action_id = sample('incident.json')['incident_id'], sample('proposal.json')['action_id']
            with store.transaction():
                store.set_state(incident_id, action_id, 'NEEDS_REVIEW', cause='approval')
            moves = [(t['seq'], t['from_state'], t['to_state'], t['cause']) for t in store.transitions(incident_id)]
            self.assertEqual(moves[-1], (3, 'VALIDATING', 'NEEDS_REVIEW', 'approval'))
            self.assertEqual(store.transitions('other-incident'), [])

    def test_limits_are_bounded(self):
        for limit in (0, 601, True, '1'):
            with self.assertRaises(ValueError):
                read_records('not-opened.db', limit=limit)
