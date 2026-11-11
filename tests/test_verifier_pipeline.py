"""Protocol-native outcome checks and replay-safe Coordinator integration."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from itertools import islice
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from experiments.boutique.harness.connected import ConnectedBoutiqueExperiment
from experiments.recovery.live import Experiment
from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import SQLiteStore, LocalEventBus, approve, handle
from services.guard.core import evaluate
from services.verifier.core import verify, VerifierInputError
from services.verifier.store import observation_seconds
from scripts.test_boutique_live import BoutiqueExperiment

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def load(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


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


def grpc_reading(execution, template, offset, **changes):
    """One healthy reading taken offset seconds after execution, plus explicit changes."""
    at = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00")) + timedelta(seconds=offset)
    return dict(grpc_samples(execution, template)[0], at=at.isoformat().replace("+00:00", "Z"), **changes)


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

    def test_template_drift_and_missing_evidence_are_never_convergence(self):
        self.policy["health"]["convergence_grace_seconds"] = 150
        row = lambda offset, **changes: grpc_reading(self.execution, self.template, offset, **changes)
        healthy = [row(offset) for offset in (6, 11, 16)]
        tolerated = verify(self.execution, [row(1, grpc_status=14)] + healthy, self.policy)
        self.assertEqual(tolerated["status"], "RECOVERED")
        self.assertTrue(tolerated["checks"][-1]["detail"].startswith("Excluded 1 "))
        later = [row(offset) for offset in (11, 16, 21)]
        for final in (True, False):
            for label, rows, counted in (
                    ("drift first", [row(1, template_sha256="b" * 64)] + healthy, False),
                    ("unhealthy drift first", [row(1, grpc_status=14, template_sha256="b" * 64)] + healthy, False),
                    ("drift after a tolerated reading", [row(1, grpc_status=14), row(6, template_sha256="b" * 64)] + later, True)):
                with self.subTest(rows=label, final=final):
                    result = verify(self.execution, rows, self.policy, final=final)
                    self.assertEqual(result["status"], "NOT_RECOVERED")
                    self.assertIn("sample_configuration", {c["name"] for c in result["checks"] if not c["passed"]})
                    check = result["checks"][-1]
                    self.assertEqual((check["name"], check["passed"]), ("convergence", not counted))
                    self.assertTrue(check["detail"].startswith("Counted 1 " if counted else "No reading was excluded"))
            for change in ({"grpc_status": None}, {"configuration_stable": False}, {"application_check_passed": None}):
                with self.subTest(change=change, final=final):
                    result = verify(self.execution, [row(1, **change)] + healthy, self.policy, final=final)
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertTrue(result["checks"][-1]["detail"].startswith("No reading was excluded"))

    def test_recorded_live_failure_shape(self):
        # The reproduced reading: shipping is healthy 8 s after the patch, but the
        # storefront's client is still reconnecting and cart/checkout fails.
        names = sorted({"adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice", "frontend",
                        "paymentservice", "productcatalogservice", "recommendationservice", "redis-cart"}
                       ) + ["storefront-checkout"]
        self.policy["health"].update(required_dependencies=names, max_sample_gap_seconds=90,
                                     verification_timeout_seconds=300)
        ready = dict.fromkeys(names, True)
        rows = [grpc_reading(self.execution, self.template, 8, dependencies=dict(ready, **{"storefront-checkout": False}))]
        rows += [grpc_reading(self.execution, self.template, offset, dependencies=dict(ready)) for offset in (16, 24, 32)]
        strict = verify(self.execution, rows, self.policy)
        self.assertEqual(strict["status"], "NOT_RECOVERED")
        self.assertNotIn("convergence", [check["name"] for check in strict["checks"]])
        self.policy["health"]["convergence_grace_seconds"] = 150
        result = verify(self.execution, rows, self.policy)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual((result["window_start"], result["window_end"]), (rows[1]["at"], rows[3]["at"]))
        detail = result["checks"][-1]["detail"]
        self.assertTrue(detail.startswith("Excluded 1 unhealthy reading(s)"))
        self.assertIn("(dependency storefront-checkout)", detail)
        self.assertEqual(verify(self.execution, rows, self.policy, final=False), result)


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

    def recover(self, pattern):
        """One opt-in recovery; pattern is the storefront-checkout health of each offered reading."""
        with tempfile.TemporaryDirectory() as root:
            experiment = BoutiqueExperiment(Path(root) / "run")
            try:
                target = dict(load("messages/proposal.json")["target"], cluster_id=experiment.cluster_id,
                              namespace=experiment.namespace, name=experiment.name)
                experiment.target = target
                experiment.policy = load("policy.json")
                experiment.policy.update(targets=[target], health=dict(
                    grpc_policy()["health"], required_dependencies=["storefront-checkout"],
                    convergence_grace_seconds=150, verification_timeout_seconds=300))
                experiment.baseline = load("baseline.json")
                experiment.baseline["target"] = target
                experiment.baseline["health_evidence"] = {"evidence_id": "HEALTH-offline-fixture"}
                deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
                deployment["metadata"].update(name=target["name"], namespace=target["namespace"], uid=target["uid"])
                with patch.object(experiment, "deployment", return_value=deployment), \
                        patch.object(experiment, "kubectl"), patch.object(experiment, "event") as event, \
                        patch.object(experiment, "verification_samples", side_effect=lambda execution: iter([
                            grpc_reading(execution, experiment.baseline["template_sha256"], 1 + 5 * index,
                                         dependencies={"storefront-checkout": healthy})
                            for index, healthy in enumerate(pattern)])):
                    incident = load("messages/incident.json")
                    incident.update(target=target, incident_id="INC-convergence")
                    handle(incident, experiment.store, experiment.bus)
                    folder, proposal = experiment.case("approved-rollback", incident, coordinator=True)
                    execution = load("messages/execution-result.json")
                    execution.update(incident_id=incident["incident_id"], action_id=proposal["action_id"], target=target,
                                     recorded_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
                    handle(execution, experiment.store, experiment.bus)
                    error = None
                    try:
                        experiment.complete_recovery(folder, proposal, execution,
                            clock=lambda: datetime.now(timezone.utc) + timedelta(seconds=20))
                    except RuntimeError as failure:
                        error = str(failure)
                    messages = [json.loads(row[0]) for row in experiment.store.db.execute("SELECT payload FROM messages")]
                    return {"error": error, "state": experiment.store.incident(incident["incident_id"])["state"],
                            "outcome": json.loads((folder / "outcome.json").read_text()),
                            "readings": json.loads((folder / "post-health.json").read_text()),
                            "checks": experiment.summary["checks"], "events": event.call_args_list,
                            "published": [message for message in messages if message["kind"] == "outcome"]}
            finally:
                experiment.store.close()

    def test_complete_recovery_waits_through_convergence(self):
        run = self.recover([False, True, True, True])
        self.assertIsNone(run["error"])
        self.assertEqual(run["state"], "RECOVERED")
        self.assertEqual(run["outcome"]["status"], "RECOVERED")
        self.assertEqual(run["published"], [run["outcome"]])
        self.assertTrue(all(check["passed"] for check in run["outcome"]["checks"]))
        disclosure = run["outcome"]["checks"][-1]
        self.assertEqual(disclosure["name"], "convergence")
        self.assertTrue(disclosure["detail"].startswith("Excluded 1 unhealthy reading(s)"))
        self.assertIn("(dependency storefront-checkout)", disclosure["detail"])
        # The excluded reading stays in the evidence; only the healthy window is claimed.
        self.assertEqual([row["dependencies"]["storefront-checkout"] for row in run["readings"]],
                         [False, True, True, True])
        self.assertEqual(run["outcome"]["window_start"], run["readings"][1]["at"])
        self.assertEqual(run["checks"][-1]["convergence"], disclosure["detail"])
        self.assertEqual(run["checks"][-1]["outcome"], "RECOVERED")

        relapse = self.recover([False, True, False])
        self.assertIn("NOT_RECOVERED (failed: ", relapse["error"])
        self.assertIn("dependency_health", relapse["error"])
        self.assertIn("convergence", relapse["error"])
        self.assertEqual(relapse["state"], "FAILED")
        self.assertEqual(relapse["outcome"]["status"], "NOT_RECOVERED")
        self.assertEqual(relapse["published"], [relapse["outcome"]])
        self.assertEqual(len(relapse["readings"]), 3)
        self.assertFalse(any("convergence" in check for check in relapse["checks"]))

        # The dashboard timeline carries the disclosure, right after the outcome and only when readings were excluded.
        summaries = [call.args[2] for call in run["events"]]
        published = summaries.index("Role 6 published RECOVERED")
        self.assertEqual(summaries[published + 1], "Role 6 disclosed convergence: " + disclosure["detail"])
        self.assertEqual(sum(summary.startswith("Role 6 disclosed convergence") for summary in summaries), 1)
        noted = run["events"][published + 1]
        self.assertEqual(noted.args[:2], (6, "status"))
        self.assertEqual(noted.kwargs, {"target": run["outcome"]["target"], "incident_id": "INC-convergence",
                                        "action_id": run["outcome"]["action_id"]})
        summaries = [call.args[2] for call in relapse["events"]]
        self.assertIn("Role 6 published NOT_RECOVERED", summaries)
        self.assertFalse(any(summary.startswith("Role 6 disclosed convergence") for summary in summaries))

    def test_live_harness_policies_opt_in_to_the_convergence_grace(self):
        # Only prepare() opts the live lab in; the cluster-facing base step is replaced.
        def offline(experiment):
            experiment.policy = {"policy_version": "offline"}

        execution = load("messages/execution-result.json")
        for kind, gap, dependencies in ((BoutiqueExperiment, 45, 0), (ConnectedBoutiqueExperiment, 90, 11)):
            with self.subTest(kind=kind.__name__), tempfile.TemporaryDirectory() as root, \
                    patch.object(Experiment, "prepare", offline):
                experiment = kind(Path(root) / "run")
                try:
                    experiment.prepare()
                    health = experiment.policy["health"]
                    self.assertEqual(json.loads((Path(root) / "run/policy.json").read_text())["health"], health)
                finally:
                    experiment.store.close()
                self.assertEqual((health["convergence_grace_seconds"], health["verification_timeout_seconds"]), (150, 300))
                self.assertEqual(observation_seconds(health), 300)
                self.assertEqual((health["minimum_samples"], health["stable_window_seconds"],
                                  health["max_sample_gap_seconds"], len(health["required_dependencies"])),
                                 (3, 10, gap, dependencies))
                # The block is a valid Role 6 policy, and the allowance is active in it.
                result = verify(execution, [], {"health": dict(health, expected_template_sha256="a" * 64)})
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["checks"][-1]["name"], "convergence")
                self.assertIn("150-second", result["checks"][-1]["detail"])

    def test_sampler_runs_until_closed_and_offers_one_reading_after_its_limit(self):
        with tempfile.TemporaryDirectory() as root:
            experiment = BoutiqueExperiment(Path(root) / "run")
            try:
                experiment.policy = {"health": dict(grpc_policy()["health"], convergence_grace_seconds=150,
                                                    verification_timeout_seconds=300)}
                deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
                execution = load("messages/execution-result.json")
                quote = {"costUsd": {"currencyCode": "USD", "units": "8", "nanos": 990000000}}
                clock = Mock()
                clock.monotonic.return_value = 1000.0
                with patch.object(experiment, "deployment", return_value=deployment) as read, \
                        patch.object(experiment, "quote_response", return_value=quote), \
                        patch.object(experiment, "observe") as observe, patch.object(experiment, "event") as event, \
                        patch("experiments.boutique.harness.live.time", clock):
                    reader = experiment.verification_samples(execution)
                    readings = list(islice(reader, 5))  # No longer capped at three readings.
                    self.assertEqual(len(readings), 5)
                    self.assertTrue(all(row["grpc_status"] == 0 and row["application_check_passed"] is True
                                        and row["service"] == experiment.name for row in readings))
                    self.assertEqual([call.args[0] for call in observe.call_args_list],
                                     ["role6-" + execution["action_id"] + "-" + str(index) for index in range(5)])
                    self.assertEqual([call.args[2] for call in event.call_args_list],
                                     ["Role 6 collected shipping verification sample " + str(index) for index in range(1, 6)])
                    self.assertEqual([call.args for call in clock.sleep.call_args_list], [(5,)] * 4)
                    collected = read.call_count
                    reader.close()  # The durable worker closes the reader once an outcome is frozen.
                    self.assertIsNone(next(reader, None))
                    self.assertEqual((read.call_count, clock.sleep.call_count), (collected, 4))

                    # The limit follows the policy's observation budget (300 s) plus 5 s.
                    clock.sleep.reset_mock()
                    reader = experiment.verification_samples(execution)
                    next(reader)
                    clock.monotonic.return_value = 1304.0
                    next(reader)
                    clock.monotonic.return_value = 1305.0
                    next(reader)  # The single reading offered at/after the fixed deadline.
                    collected = read.call_count
                    self.assertIsNone(next(reader, None))
                    self.assertEqual((read.call_count, clock.sleep.call_count), (collected, 2))
            finally:
                experiment.store.close()

    def test_sampler_limit_follows_the_policy_budget_and_is_decided_before_collecting(self):
        with tempfile.TemporaryDirectory() as root:
            experiment = BoutiqueExperiment(Path(root) / "run")
            try:
                deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
                execution = load("messages/execution-result.json")
                quote = {"costUsd": {"currencyCode": "USD", "units": "8", "nanos": 990000000}}
                clock = Mock()
                with patch.object(experiment, "deployment", return_value=deployment) as read, \
                        patch.object(experiment, "quote_response", return_value=quote), \
                        patch.object(experiment, "observe"), patch.object(experiment, "event"), \
                        patch("experiments.boutique.harness.live.time", clock):
                    # A longer budget moves the limit with it: budget + 5 s after the reader starts.
                    for health, budget in (({"verification_timeout_seconds": 400}, 400),
                                           ({"stable_window_seconds": 20, "max_sample_gap_seconds": 30}, 260)):
                        with self.subTest(budget=budget):
                            experiment.policy = {"health": dict(grpc_policy()["health"],
                                                                convergence_grace_seconds=150, **health)}
                            self.assertEqual(observation_seconds(experiment.policy["health"]), budget)
                            clock.monotonic.return_value = 1000.0
                            reader = experiment.verification_samples(execution)
                            self.assertIsNotNone(next(reader, None))
                            clock.monotonic.return_value = 1004.0 + budget
                            self.assertIsNotNone(next(reader, None))  # Still inside the limit.
                            clock.monotonic.return_value = 1005.0 + budget
                            self.assertIsNotNone(next(reader, None))  # The single reading at the limit.
                            self.assertIsNone(next(reader, None))

                    # A reading that was already being collected when the limit passed is not
                    # the last one: the ledger may have judged it before its fixed deadline.
                    def slow():
                        clock.monotonic.return_value = 5000.0
                        return deployment

                    experiment.policy["health"]["verification_timeout_seconds"] = 300
                    clock.monotonic.return_value = 1000.0
                    read.side_effect = slow
                    read.reset_mock()
                    reader = experiment.verification_samples(execution)
                    self.assertEqual(len(list(reader)), 2)
                    self.assertEqual(read.call_count, 4)
            finally:
                experiment.store.close()

    def test_sampler_skips_a_failed_cluster_read_and_stops_after_three_in_a_row(self):
        with tempfile.TemporaryDirectory() as root:
            experiment = BoutiqueExperiment(Path(root) / "run")
            try:
                experiment.policy = {"health": dict(grpc_policy()["health"], convergence_grace_seconds=150,
                                                    verification_timeout_seconds=300)}
                deployment = load("observations.json")["failed-rollout"][-1]["deployment"]
                execution = load("messages/execution-result.json")
                quote = {"costUsd": {"currencyCode": "USD", "units": "8", "nanos": 990000000}}
                failure = RuntimeError("kubectl operation failed: get")
                clock = Mock()
                clock.monotonic.return_value = 1000.0
                with patch.object(experiment, "deployment") as read, \
                        patch.object(experiment, "quote_response", return_value=quote), \
                        patch.object(experiment, "observe"), patch.object(experiment, "event") as event, \
                        patch("experiments.boutique.harness.live.time", clock):
                    def skipped():
                        return [c for c in event.call_args_list if c.args[:2] == (6, "error")]

                    # A failed read before or after the probe skips only that reading.
                    read.side_effect = [deployment, deployment, failure, deployment, deployment,
                                        deployment, failure, deployment, deployment]
                    readings = list(islice(experiment.verification_samples(execution), 3))
                    self.assertEqual(len(readings), 3)
                    self.assertEqual(len(skipped()), 2)
                    self.assertIn("cluster read failed: RuntimeError", skipped()[0].args[2])
                    self.assertEqual(skipped()[0].kwargs["action_id"], execution["action_id"])

                    # A successful reading resets the count; three failures in a row raise.
                    event.reset_mock()
                    read.side_effect = [failure, failure, deployment, deployment, failure, failure, failure]
                    reader = experiment.verification_samples(execution)
                    self.assertIsNotNone(next(reader))
                    with self.assertRaisesRegex(RuntimeError, "kubectl operation failed"):
                        next(reader)
                    self.assertEqual(len(skipped()), 5)
            finally:
                experiment.store.close()


if __name__ == "__main__":
    unittest.main()
