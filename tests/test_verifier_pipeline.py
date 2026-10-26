"""Protocol-native outcome checks and replay-safe Coordinator integration."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator import SQLiteStore, LocalEventBus, approve, handle
from services.guard import evaluate
from services.verifier import verify, VerifierInputError
from scripts.test_boutique_live import BoutiqueExperiment

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def load(name):
    return json.loads((ROOT / "samples" / name).read_text(encoding="utf-8"))


def grpc_policy(template=None):
    health = {"protocol": "grpc", "required_grpc_status": 0, "minimum_samples": 3,
              "stable_window_seconds": 10, "max_sample_gap_seconds": 45,
              "required_dependencies": [], "require_target_identity": True,
              "require_template_match": True, "require_available_replicas": True}
    if template is not None:
        health["expected_template_sha256"] = template
    return {"health": health}


def grpc_samples(execution, template):
    at = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
    return [{"at": (at + timedelta(seconds=offset)).isoformat().replace("+00:00", "Z"),
             "service": execution["target"]["name"], "target": deepcopy(execution["target"]),
             "protocol": "grpc", "grpc_status": 0, "application_check_passed": True,
             "ready_replicas": 1, "desired_replicas": 1, "available_replicas": 1,
             "dependencies": {}, "configuration_stable": True, "template_sha256": template}
            for offset in (1, 6, 11)]


class GrpcVerifierTests(unittest.TestCase):
    def setUp(self):
        self.execution = load("messages/execution-result.json")
        self.template = load("messages/proposal.json")["parameters"]["template_sha256"]
        self.policy = grpc_policy(self.template)
        self.samples = grpc_samples(self.execution, self.template)

    def test_real_protocol_fields_are_required_instead_of_fake_http_status(self):
        self.assertEqual(verify(self.execution, self.samples, self.policy)["status"], "RECOVERED")
        for change in ({"grpc_status": None, "http_status": 200}, {"application_check_passed": None},
                       {"grpc_status": True}, {"protocol": "http"}, {"target": None},
                       {"configuration_stable": False}, {"available_replicas": None},
                       {"template_sha256": None}):
            with self.subTest(change=change):
                samples = deepcopy(self.samples)
                samples[1].update(change)
                self.assertEqual(verify(self.execution, samples, self.policy)["status"], "UNKNOWN")

    def test_known_grpc_readiness_application_and_configuration_failures_are_not_recovered(self):
        for change in ({"grpc_status": 14}, {"application_check_passed": False},
                       {"ready_replicas": 0}, {"available_replicas": 0}, {"template_sha256": "b" * 64}):
            with self.subTest(change=change):
                samples = deepcopy(self.samples)
                samples[1].update(change)
                self.assertEqual(verify(self.execution, samples, self.policy)["status"], "NOT_RECOVERED")

    def test_full_target_identity_cannot_be_omitted_or_replaced(self):
        for replacement in (None, dict(self.execution["target"], uid="recreated")):
            samples = deepcopy(self.samples)
            if replacement is None:
                del samples[1]["target"]
            else:
                samples[1]["target"] = replacement
            self.assertEqual(verify(self.execution, samples, self.policy)["status"], "UNKNOWN")

    def test_incomplete_or_gapped_health_window_is_unknown(self):
        self.assertEqual(verify(self.execution, self.samples[:1], self.policy)["status"], "UNKNOWN")
        self.samples[2]["at"] = "2026-10-10T12:05:00Z"
        self.assertEqual(verify(self.execution, self.samples, self.policy)["status"], "UNKNOWN")

    def test_invalid_grpc_policy_cannot_authorize_recovery(self):
        for change in ({"required_grpc_status": 14}, {"protocol": "unknown"},
                       {"require_target_identity": "true"}, {"expected_template_sha256": None}):
            policy = deepcopy(self.policy)
            policy["health"].update(change)
            with self.subTest(change=change), self.assertRaises(VerifierInputError):
                verify(self.execution, self.samples, policy)


class VerifierWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        self.proposal = load("messages/proposal.json")
        self.execution = load("messages/execution-result.json")
        policy, baseline = load("policy.json"), load("baseline.json")
        state = load("observations.json")["failed-rollout"][-1]
        dependencies = load("messages/dependency-result.json")
        for message in (load("messages/incident.json"), self.proposal, dependencies):
            handle(message, self.store, self.bus)
        handle(evaluate(self.proposal, state, policy, baseline, dependencies, now=NOW), self.store, self.bus)
        approval = approve(self.proposal["action_id"], "test-operator", self.store)
        handle(evaluate(self.proposal, state, policy, baseline, dependencies, approval,
                        now=NOW + timedelta(seconds=1)), self.store, self.bus)
        handle(self.execution, self.store, self.bus)
        self.samples = grpc_samples(self.execution, self.proposal["parameters"]["template_sha256"])

    def process(self, reader):
        return process_verifier_requests(self.store, self.bus, sample_reader=reader,
                                         policy_reader=lambda execution: grpc_policy())

    def state(self):
        return self.store.incident(self.execution["incident_id"])["state"]

    def test_verified_outcome_closes_incident_and_duplicate_delivery_does_not_recollect(self):
        self.assertEqual(self.state(), "VERIFYING")
        self.assertEqual(self.process(lambda execution: self.samples)[0]["status"], "RECOVERED")
        self.assertEqual(self.state(), "RECOVERED")
        self.store.db.execute("UPDATE outbox SET delivered=0 WHERE topic='verifier.request'")
        self.assertEqual(self.process(lambda execution: self.fail("duplicate collection")), [])
        self.assertEqual(self.state(), "RECOVERED")

    def test_failed_application_publishes_failed_incident(self):
        self.samples[1]["application_check_passed"] = False
        self.assertEqual(self.process(lambda execution: self.samples)[0]["status"], "NOT_RECOVERED")
        self.assertEqual(self.state(), "FAILED")

    def test_collection_error_publishes_unknown_and_needs_review(self):
        def unavailable(execution):
            raise TimeoutError("not a measurement")
        outcome = self.process(unavailable)[0]
        self.assertEqual(outcome["status"], "UNKNOWN")
        self.assertEqual(self.state(), "NEEDS_REVIEW")
        self.assertTrue(any(c["name"] == "evidence_collection" and not c["passed"] for c in outcome["checks"]))

    def test_sample_collection_holds_no_coordinator_transaction(self):
        def collect(execution):
            self.assertFalse(self.store.db.in_transaction)
            return self.samples
        self.process(collect)

    def test_stale_request_payload_cannot_verify_another_execution(self):
        bad = deepcopy(self.execution)
        bad["resource_version"] = "different"
        self.store.db.execute("UPDATE outbox SET payload=? WHERE topic='verifier.request'", (json.dumps({"execution": bad}),))
        self.assertEqual(self.process(lambda execution: self.fail("stale request collected")), [])
        self.assertEqual(self.state(), "VERIFYING")

    def test_concurrent_lifecycle_change_is_not_overwritten(self):
        def collect(execution):
            handle(verify(execution, [], grpc_policy(self.proposal["parameters"]["template_sha256"])), self.store, self.bus)
            return self.samples
        self.assertEqual(self.process(collect), [])
        self.assertEqual(self.state(), "NEEDS_REVIEW")

    def test_outcome_record_and_request_ack_roll_back_together(self):
        with patch("runtime.verifier_pipeline.record_message", side_effect=RuntimeError("storage")):
            with self.assertRaises(RuntimeError):
                self.process(lambda execution: self.samples)
        self.assertEqual(self.state(), "VERIFYING")
        pending = self.store.db.execute("SELECT delivered FROM outbox WHERE topic='verifier.request'").fetchone()[0]
        self.assertEqual(pending, 0)
        self.assertEqual(self.process(lambda execution: self.samples)[0]["status"], "RECOVERED")


class BoutiqueRoleSixIntegrationTests(unittest.TestCase):
    def test_both_boutique_actions_publish_real_dependency_and_outcome_messages(self):
        with tempfile.TemporaryDirectory() as root:
            experiment = BoutiqueExperiment(Path(root) / "run")
            try:
                target = dict(load("messages/proposal.json")["target"], cluster_id=experiment.cluster_id,
                              namespace=experiment.namespace, name=experiment.name)
                experiment.target = target
                experiment.policy = load("policy.json")
                experiment.policy.update(targets=[target], health=grpc_policy()["health"])
                experiment.baseline = load("baseline.json")
                experiment.baseline["target"] = target
                experiment.baseline["health_evidence"] = {"evidence_id": "HEALTH-offline-fixture"}
                deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
                deployment["metadata"].update(name=target["name"], namespace=target["namespace"], uid=target["uid"])
                with patch.object(experiment, "deployment", return_value=deployment), \
                        patch.object(experiment, "kubectl"), \
                        patch.object(experiment, "verification_samples", side_effect=lambda execution:
                                     grpc_samples(execution, experiment.baseline["template_sha256"])):
                    for index, label in enumerate(("approved-rollback", "crash-after-write")):
                        incident = load("messages/incident.json")
                        incident.update(target=target, incident_id="INC-integration-" + str(index))
                        handle(incident, experiment.store, experiment.bus)
                        folder, proposal = experiment.case(label, incident, coordinator=True)
                        execution = load("messages/execution-result.json")
                        execution.update(incident_id=incident["incident_id"], action_id=proposal["action_id"], target=target,
                                         recorded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                        handle(execution, experiment.store, experiment.bus)
                        experiment.complete_recovery(folder, proposal, execution,
                            clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=20))
                        self.assertEqual(experiment.store.incident(incident["incident_id"])["state"], "RECOVERED")
                        self.assertEqual(json.loads((folder / "outcome.json").read_text())["status"], "RECOVERED")
                    messages = [json.loads(row[0]) for row in experiment.store.db.execute("SELECT payload FROM messages")]
                    self.assertEqual(sum(m["kind"] == "dependency_result" for m in messages), 2)
                    self.assertEqual(sum(m["kind"] == "outcome" for m in messages), 2)
            finally:
                experiment.store.close()


if __name__ == "__main__":
    unittest.main()
