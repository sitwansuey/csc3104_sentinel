"""Real local Planner/Coordinator handoff with provider failure and crash injection."""
from copy import deepcopy
from datetime import timedelta
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.validation.demo_offline_flow import OfflineFlow, fixture
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance
from services.coordinator.core import SQLiteStore, handle
from services.observer.core import detect
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from services.planner.worker import escalation


class DurablePlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.f = OfflineFlow(self.directory.name)
        self.incident = detect(fixture("observations.json")["failed-rollout"], self.f.policy)
        handle(self.incident, self.f.store, self.f.bus)
        self.path = self.f.path / "planner.db"
        self.journal = PlannerStore(self.path, clock=lambda: self.f.now)
        self.provider = Mock(side_effect=mock_provider)

    def tearDown(self):
        self.journal.close()
        self.f.close()
        self.directory.cleanup()

    def restart(self):
        self.journal.close()
        self.f.restart()
        self.journal = PlannerStore(self.path, clock=lambda: self.f.now)

    def plan(self, **options):
        return process_planner_requests(self.f.store, self.f.bus,
            baseline_reader=lambda i: [self.f.baseline], retriever=retrieve_runbooks,
            provider=self.provider, journal=self.journal, **options)

    def step(self):
        never = lambda *args: self.fail("Pending or escalated planning must not authorize a repair")
        return advance(self.f.store, self.incident["incident_id"], plan=self.plan,
            assess=never, guard=self.f.guard, approve=never, execute=never, verify=never)

    def test_saved_proposal_replays_after_delivery_crash_without_model_recall(self):
        with patch("runtime.planner_pipeline.record_message", side_effect=OSError("lost result")):
            with self.assertRaises(OSError):
                self.plan()
        proposal = self.journal.get(self.incident["incident_id"])["proposal"]
        self.assertEqual(self.f.store.incident(self.incident["incident_id"])["state"], "OPEN")
        self.restart()
        self.assertEqual(self.plan(), [proposal])
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.provider.call_count, 1)
        attempts = self.journal.attempts(self.incident["incident_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["context"]["incident"], self.incident)
        self.assertEqual(attempts[0]["context"]["baselines"], [self.f.baseline])
        self.assertEqual(attempts[0]["context"]["runbooks"][0]["id"], "failed-rollout")
        self.assertEqual(self.f.store.action(proposal["action_id"])["proposal"], proposal)
        self.assertEqual(self.f.cluster.writes, [])

    def test_retry_backoff_survives_restart_then_recovers(self):
        self.provider.side_effect = TimeoutError("SECRET provider detail")
        self.assertEqual(self.step(), "OPEN")
        saved = self.journal.get(self.incident["incident_id"])
        self.assertEqual(saved["attempts"], 1)
        self.assertEqual(saved["error_code"], "PROVIDER_UNAVAILABLE")
        self.assertNotIn("SECRET", json.dumps(saved))
        self.assertEqual(len(self.f.store.pending_routes("planner.request")), 1)
        self.restart()
        self.assertEqual(self.step(), "OPEN")
        self.assertEqual(self.provider.call_count, 1)
        self.f.now += timedelta(seconds=5)
        self.provider.side_effect = mock_provider
        proposal = self.plan()[0]
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual(self.journal.get(self.incident["incident_id"])["attempts"], 2)

    def test_lost_delivery_marker_reconciles_from_committed_coordinator_result(self):
        with patch.object(self.journal, "delivered", side_effect=OSError("marker interrupted")):
            with self.assertRaises(OSError):
                self.plan()
        self.assertFalse(self.journal.get(self.incident["incident_id"])["delivered"])
        self.restart()
        self.assertEqual(self.plan(), [])
        self.assertTrue(self.journal.get(self.incident["incident_id"])["delivered"])
        self.assertEqual(self.provider.call_count, 1)

    def test_invalid_output_exhausts_to_persisted_escalation_without_a_write(self):
        self.provider.side_effect = lambda context: {}
        self.assertEqual(self.step(), "OPEN")
        self.f.now += timedelta(seconds=5)
        self.assertEqual(self.step(), "OPEN")
        self.restart()
        self.f.now += timedelta(seconds=10)
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        saved = self.journal.get(self.incident["incident_id"])
        self.assertEqual(saved["attempts"], 3)
        self.assertEqual(saved["proposal"]["action"], "ESCALATE")
        self.assertEqual(saved["error_code"], "INVALID_PROPOSAL")
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assertEqual(self.provider.call_count, 3)
        self.assertEqual(self.f.cluster.writes, [])
        self.assertEqual(self.f.store.pending_routes("planner.request"), [])

    def test_process_death_during_last_attempt_is_bounded_and_fenced(self):
        token, exhausted = self.journal.claim(self.incident, max_attempts=1, lease_seconds=10)
        self.assertFalse(exhausted)
        self.restart()
        self.assertEqual(self.plan(max_attempts=1, lease_seconds=10), [])
        self.f.now += timedelta(seconds=10)
        proposal = self.plan(max_attempts=1, lease_seconds=10)[0]
        self.assertEqual(proposal["action"], "ESCALATE")
        self.provider.assert_not_called()
        with self.assertRaises(ValueError):
            self.journal.save(self.incident["incident_id"], token, proposal)
        self.assertEqual(self.journal.get(self.incident["incident_id"])["attempts"], 1)

    def test_other_worker_cannot_claim_a_leased_request(self):
        token, _ = self.journal.claim(self.incident, max_attempts=3, lease_seconds=10)
        with PlannerStore(self.path, clock=lambda: self.f.now) as other:
            self.assertIsNone(other.claim(self.incident, max_attempts=3, lease_seconds=10))
            self.f.now += timedelta(seconds=10)
            replacement, _ = other.claim(self.incident, max_attempts=3, lease_seconds=10)
            with self.assertRaises(ValueError):
                self.journal.retry(self.incident["incident_id"], token,
                                   error_code="PROVIDER_UNAVAILABLE", delay_seconds=5)
            self.assertNotEqual(token, replacement)

    def test_provider_does_not_hold_coordinator_write_transaction(self):
        def infer(context):
            self.assertFalse(self.f.store.in_transaction)
            with SQLiteStore(self.f.path / "coordinator.db") as other:
                with other.transaction():
                    other.db.execute("UPDATE incidents SET state=state")
            return mock_provider(context)
        self.provider.side_effect = infer
        self.assertEqual(self.plan()[0]["action"], "ROLLBACK")

    def test_legacy_provider_allows_another_incident_to_arrive_during_inference(self):
        second = dict(self.incident, incident_id="INC-DURING-INFERENCE")
        def infer(context):
            self.assertFalse(self.f.store.in_transaction)
            with SQLiteStore(self.f.path / "coordinator.db") as other:
                handle(second, other, self.f.bus)
            return mock_provider(context)
        proposals = process_planner_requests(self.f.store, self.f.bus,
            baseline_reader=lambda _: [self.f.baseline], retriever=retrieve_runbooks, provider=infer)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(self.f.store.incident(second["incident_id"])["state"], "OPEN")
        self.assertEqual(self.plan(), [])
        self.assertEqual(len(self.f.store.pending_routes("planner.request")), 1)

    def test_natural_escalation_is_recorded_without_operator_approval(self):
        self.f.baseline = dict(self.f.baseline, template_sha256="0" * 64)
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.provider.assert_not_called()
        self.assertEqual(self.f.cluster.writes, [])

    def abstain(self, context):
        return {"action": "ESCALATE", "parameters": {},
                "rationale": "Evidence does not support a supported repair.",
                "evidence_refs": [item["evidence_id"] for item in context["incident"]["signals"]]}

    def test_model_abstention_is_saved_delivered_once_and_never_authorizes_a_write(self):
        self.provider.side_effect = self.abstain
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        saved = self.journal.get(self.incident["incident_id"])
        self.assertEqual(saved["proposal"]["action"], "ESCALATE")
        # The model's own answer is kept, not replaced by the exhausted-attempts escalation.
        self.assertEqual(saved["proposal"]["rationale"], "Evidence does not support a supported repair.")
        self.assertNotEqual(saved["proposal"], escalation(self.incident))
        self.assertEqual(self.f.store.action(saved["proposal"]["action_id"])["proposal"], saved["proposal"])
        self.assertEqual(saved["attempts"], 1)
        self.assertIsNone(saved["error_code"])  # An answer, not a provider failure.
        self.assertTrue(saved["delivered"])
        self.restart()
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.step(), "NEEDS_REVIEW")
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.f.cluster.writes, [])
        self.assertEqual(self.f.store.pending_routes("planner.request"), [])

    def test_model_abstention_replays_after_delivery_crash_without_model_recall(self):
        self.provider.side_effect = self.abstain
        with patch("runtime.planner_pipeline.record_message", side_effect=OSError("lost result")):
            with self.assertRaises(OSError):
                self.plan()
        saved = self.journal.get(self.incident["incident_id"])["proposal"]
        self.assertEqual(saved["action"], "ESCALATE")
        self.assertEqual(saved["rationale"], "Evidence does not support a supported repair.")
        self.assertEqual(self.f.store.incident(self.incident["incident_id"])["state"], "OPEN")
        self.restart()
        self.assertEqual(self.plan(), [saved])
        self.assertEqual(self.provider.call_count, 1)
        self.assertEqual(self.f.store.action(saved["action_id"])["proposal"], saved)
        self.assertEqual(self.f.cluster.writes, [])

    def exhaust_with_failing_input(self, **readers):
        options = {"baseline_reader": lambda i: [self.f.baseline], "retriever": retrieve_runbooks, **readers}
        def plan():
            return process_planner_requests(self.f.store, self.f.bus, provider=self.provider,
                                            journal=self.journal, **options)
        self.assertEqual(plan(), [])
        self.f.now += timedelta(seconds=5)
        self.assertEqual(plan(), [])
        self.restart()
        self.f.now += timedelta(seconds=10)
        proposal = plan()[0]
        saved = self.journal.get(self.incident["incident_id"])
        self.assertEqual(proposal["action"], "ESCALATE")
        self.assertEqual(saved["attempts"], 3)
        self.assertEqual(saved["error_code"], "PROVIDER_UNAVAILABLE")
        self.provider.assert_not_called()
        self.assertEqual(self.f.store.incident(self.incident["incident_id"])["state"], "NEEDS_REVIEW")
        self.assertEqual(self.f.cluster.writes, [])

    def test_retrieval_failures_are_bounded_and_end_in_escalation(self):
        def retriever(query):
            raise TimeoutError("runbook store unavailable")
        self.exhaust_with_failing_input(retriever=retriever)

    def test_baseline_read_failures_are_bounded_and_end_in_escalation(self):
        def baseline_reader(incident):
            raise OSError("baseline store unavailable")
        self.exhaust_with_failing_input(baseline_reader=baseline_reader)

    def test_changed_incident_payload_cannot_reuse_a_saved_result(self):
        self.journal.claim(self.incident, max_attempts=3, lease_seconds=10)
        changed = deepcopy(self.incident)
        changed["observed_resource_version"] = "changed"
        with self.assertRaises(ValueError):
            self.journal.claim(changed, max_attempts=3, lease_seconds=10)

    def test_invalid_retry_and_lease_configuration_is_rejected(self):
        for seconds in (0, -1, float("inf"), float("nan"), True):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.plan(retry_seconds=seconds)
        for options in ({"max_attempts": 0}, {"max_attempts": True}, {"lease_seconds": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.plan(**options)
