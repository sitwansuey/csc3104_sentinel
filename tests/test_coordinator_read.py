import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.dashboard import snapshot
from services.coordinator.core import SQLiteStore, LocalEventBus, handle
from services.coordinator_read import read_records


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

    def test_limits_are_bounded(self):
        for limit in (0, 601, True, '1'):
            with self.assertRaises(ValueError):
                read_records('not-opened.db', limit=limit)
