"""Optional approval expiry connects Coordinator records to strict live Guard."""

from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import unittest

from scripts.demo_guard_v2 import build_inputs
from services.coordinator.core import CoordinatorError, LocalEventBus, SQLiteStore, approve, handle
from services.guard.core import evaluate


class CoordinatorExpiryTests(unittest.TestCase):
    def setUp(self):
        self.inputs = build_inputs()
        self.now = self.inputs.pop("now")
        self.inputs["approval"] = None
        self.store = SQLiteStore(clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        incident = json.loads((Path(__file__).resolve().parents[1] /
                               "data/sentinel/samples/messages/incident.json").read_text(encoding="utf-8"))
        handle(incident, self.store, self.bus)
        handle(self.inputs["proposal"], self.store, self.bus)
        decision = evaluate(**self.inputs, now=self.now)
        self.assertEqual(decision["reason_codes"], ["OPERATOR_APPROVAL_REQUIRED"])
        handle(decision, self.store, self.bus)
        self.action_id = self.inputs["proposal"]["action_id"]

    def test_expiring_record_is_persisted_replayable_and_accepted_by_live_guard(self):
        expiry = (self.now + timedelta(seconds=60)).isoformat()
        record = approve(self.action_id, "local-operator", self.store, expires_at=expiry)
        self.assertEqual(record, self.store.approval(self.action_id))
        self.assertEqual(record["expires_at"], expiry)
        self.assertEqual(approve(self.action_id, "local-operator", self.store, expires_at=expiry), record)
        self.inputs["approval"] = deepcopy(record)
        self.assertEqual(evaluate(**self.inputs, now=self.now)["verdict"], "ALLOW")

    def test_invalid_expiry_never_creates_an_approval(self):
        for expiry in ("invalid", "2026-10-10T12:00:40", self.now.isoformat(),
                       (self.now - timedelta(seconds=1)).isoformat(), 123):
            with self.subTest(expiry=expiry), self.assertRaises(CoordinatorError):
                approve(self.action_id, "local-operator", self.store, expires_at=expiry)
            self.assertIsNone(self.store.approval(self.action_id))

    def test_changed_expiry_is_not_an_identical_approval_retry(self):
        approve(self.action_id, "local-operator", self.store,
                expires_at=(self.now + timedelta(seconds=60)).isoformat())
        with self.assertRaises(CoordinatorError):
            approve(self.action_id, "local-operator", self.store,
                    expires_at=(self.now + timedelta(seconds=120)).isoformat())
