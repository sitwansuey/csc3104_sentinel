"""A Recreate rollout has no Pod when the Verifier's first reading lands (sentinel-dist, 13 Nov 2026).

In S0 r1 of the third sentinel-dist deployment (16:38-16:45Z) the Executor
rolled demo-api back with one write, and the Verifier's first reading, about
6 s later, found no Pod: the Deployment uses Recreate, so the old Pod goes
before the new one starts. The HTTP probe got no response, which made that
reading incomplete. Every later reading, from about +13 s to +84 s, was
healthy, yet the outcome was UNKNOWN (service_health failed) and the incident
went to NEEDS_REVIEW: the convergence grace set aside only complete, unhealthy
leading readings, and one incomplete reading kept the whole window UNKNOWN.

The collector now records the Deployment's rollout counts with each reading,
and inside the grace a reading with no response, no ready or available
replica and those counts is judged as the unhealthy reading it is
(services/verifier/core.py::_awaiting_rollout). Everything else about it must
still be complete. At b9e4ddb RecreateTimelineTests and the live readings in
LiveReadingsTests give UNKNOWN / NEEDS_REVIEW.
"""
from copy import deepcopy
from datetime import datetime, timedelta, UTC
import unittest

from experiments.distributed.kind_acceptance import target_inputs
from experiments.distributed.kind_bundle import HEALTH as KIND_HEALTH
from services.verifier.core import AWAITING_ROLLOUT_CAUSE, ROLLOUT_FIELDS, reading_status, verify
from services.verifier.live import LiveEvidenceCollector
from shared.contracts import validate_message
from tests.test_verifier_live import HTTP, Kubernetes, configuration
from tests.test_verifier_sampling import SimLab

TARGET = {"cluster_id": "sentinel-dist", "kind": "Deployment", "name": "demo-api",
          "namespace": "sentinel-dist-workload", "uid": "f2383e96-6e88-4c11-9b15-a1d560eb6edc"}
TEMPLATE = {"metadata": {"labels": {"app": "demo-api"}},
            "spec": {"containers": [{"name": "api", "image": "docker.io/library/demo@sha256:" + "a" * 64}]}}


def dist_policy():
    """The policy.json the sentinel-dist lab deploys (experiments/distributed/kind_acceptance.py::target_inputs)."""
    window = [{"at": "2026-11-13T16:30:00Z"}]
    policy = target_inputs(TARGET, TEMPLATE, window)["policy.json"]
    policy["health"]["expected_template_sha256"] = "b" * 64  # The proposal's, as VerifierAdapter sets it.
    return policy


def execution(recorded_at="2026-11-13T16:39:54.621009Z"):
    message = {"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-DIST-R1",
               "action_id": "ACT-5d8258af5de924c29c340514", "target": deepcopy(TARGET), "status": "APPLIED",
               "resource_version": "42726", "recorded_at": recorded_at,
               "details": "[basis=EVIDENCE_MATCH] write confirmed"}
    validate_message(message)
    return message


def reading(at, *, answered=True, facts=True, **changes):
    """One reading as the live collector builds it for demo-api; answered=False is the Pod-less one."""
    row = {"at": at, "service": "demo-api", "target": deepcopy(TARGET),
           "http_status": 200 if answered else None, "ready_replicas": 1 if answered else 0,
           "desired_replicas": 1, "available_replicas": 1 if answered else 0,
           "template_sha256": "b" * 64, "configuration_stable": True, "dependencies": {},
           "source": "live_verifier", "dependency_unknowns": []}
    if facts:
        row.update(generation=4, observed_generation=4, updated_replicas=1, total_replicas=1 if answered else 0)
    row.update(changes)
    return row


# The readings verifier-1 saved for S0 r1, read from its store without writing (seconds after 16:40:00Z).
LIVE_TIMES = ("00.651605", "07.712259", "17.785446", "27.850575", "38.262966", "48.694937", "58.795866")


def live_readings(facts=True):
    rows = [reading("2026-11-13T16:40:" + LIVE_TIMES[0] + "Z", answered=False, facts=facts)]
    rows += [reading("2026-11-13T16:40:" + at + "Z", facts=facts) for at in LIVE_TIMES[1:]]
    rows += [reading("2026-11-13T16:41:08.891000Z", facts=facts), reading("2026-11-13T16:41:18.979272Z", facts=facts)]
    return rows


def convergence(result):
    [check] = [check for check in result["checks"] if check["name"] == "convergence"]
    return check


class LiveReadingsTests(unittest.TestCase):
    """verify() over the r1 readings, with and without the rollout counts the collector now records."""

    def setUp(self):
        self.policy, self.execution = dist_policy(), execution()

    def test_the_r1_window_recovers_once_the_pod_less_first_reading_carries_its_rollout_counts(self):
        rows = live_readings()
        self.assertEqual(reading_status(self.execution, rows[0], self.policy), "incomplete")
        self.assertEqual(verify(self.execution, rows, self.policy, final=False)["status"], "RECOVERED")
        result = verify(self.execution, rows, self.policy)
        self.assertEqual(result["status"], "RECOVERED")
        self.assertTrue(all(check["passed"] for check in result["checks"]))
        self.assertEqual(result["window_start"], rows[1]["at"])
        detail = convergence(result)["detail"]
        self.assertTrue(detail.startswith("Excluded 1 unhealthy reading(s) from 2026-11-13T16:40:00.651605Z"), detail)
        self.assertIn("(" + AWAITING_ROLLOUT_CAUSE + ")", detail)
        # The strict verdict on the readings as they are is unchanged: the silent one is missing evidence.
        strict = deepcopy(self.policy)
        del strict["health"]["convergence_grace_seconds"]
        self.assertEqual(verify(self.execution, rows, strict)["status"], "UNKNOWN")

    def test_the_readings_as_saved_live_without_rollout_counts_stay_unknown(self):
        result = verify(self.execution, live_readings(facts=False), self.policy)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual([check["name"] for check in result["checks"] if not check["passed"]], ["service_health"])
        self.assertTrue(convergence(result)["detail"].startswith("No reading was excluded"))

    def test_any_other_gap_in_the_silent_reading_still_counts(self):
        """Identity, template, timestamp, stability, collector errors and a ready replica are never set aside."""
        changes = {
            "another uid": {"target": dict(TARGET, uid="recreated")},
            "another service": {"service": "other"},
            "unapproved template": {"template_sha256": "c" * 64},
            "unreadable template": {"template_sha256": None},
            "configuration not stable": {"configuration_stable": False},
            "collector failed the Deployment read": {"ready_replicas": None, "desired_replicas": None,
                                                     "available_replicas": None},
            "dependency evidence missing": {"dependencies": None},
            "a replica is ready": {"ready_replicas": 1},
            "a replica is available": {"available_replicas": 1},
            "availability unknown": {"available_replicas": None},
            "another protocol": {"protocol": "grpc"},
            "a malformed response": {"http_status": "none"},
        }
        for field in ROLLOUT_FIELDS:
            changes[f"{field} missing"] = {field: None}
            changes[f"{field} negative"] = {field: -1}
            changes[f"{field} not an integer"] = {field: True}
        changes["generation zero"] = {"generation": 0}
        for label, change in changes.items():
            rows = live_readings()
            rows[0].update(change)
            for final in (True, False):
                with self.subTest(label, final=final):
                    result = verify(self.execution, rows, self.policy, final=final)
                    self.assertEqual(result["status"], "UNKNOWN")
                    self.assertTrue(convergence(result)["detail"].startswith("No reading was excluded"))

    def test_an_unstamped_or_reordered_silent_reading_still_counts(self):
        for at in (None, "2026-11-13T16:40:00", "2026-11-13T16:39:50Z", "2026-11-13T16:40:07.712259Z"):
            with self.subTest(at=at):
                rows = live_readings()
                rows[0]["at"] = at
                self.assertEqual(verify(self.execution, rows, self.policy)["status"], "UNKNOWN")

    def test_a_silent_reading_after_the_grace_or_after_a_healthy_one_always_counts(self):
        late = execution("2026-11-13T16:38:59.000000Z")  # The first reading is then 61.65 s after the write.
        self.assertEqual(verify(late, live_readings(), self.policy)["status"], "UNKNOWN")
        rows = live_readings()
        rows.insert(3, reading("2026-11-13T16:40:20.000000Z", answered=False))
        for final in (True, False):
            with self.subTest(relapse=True, final=final):
                self.assertEqual(verify(self.execution, rows, self.policy, final=final)["status"], "UNKNOWN")

    def test_a_silent_target_that_never_answers_or_answers_unhealthy_ends_as_today(self):
        # Silent throughout, the last readings after the 60 s grace: UNKNOWN, as before the change.
        stamps = [datetime(2026, 11, 13, 16, 40, tzinfo=UTC) + timedelta(seconds=10 * index)
                  for index in range(9)]
        silent = [reading(at.isoformat().replace("+00:00", "Z"), answered=False) for at in stamps]
        self.assertEqual(verify(self.execution, silent, self.policy)["status"], "UNKNOWN")
        self.assertEqual(verify(self.execution, silent, self.policy, final=False)["status"], "UNKNOWN")
        # A Pod that answers 503 after the silent start: still UNKNOWN, never RECOVERED.
        unhealthy = silent[:1] + [dict(row, http_status=503, total_replicas=1) for row in silent[1:]]
        self.assertEqual(verify(self.execution, unhealthy, self.policy)["status"], "UNKNOWN")

    def test_a_policy_without_grace_is_unchanged(self):
        policy = deepcopy(self.policy)
        del policy["health"]["convergence_grace_seconds"]
        for final in (True, False):
            with self.subTest(final=final):
                result = verify(self.execution, live_readings(), policy, final=final)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertNotIn("convergence", [check["name"] for check in result["checks"]])

    def test_grpc_and_redis_targets_waiting_for_their_pod_are_judged_the_same_way(self):
        for protocol, silent, answered in (
                ("grpc", {"grpc_status": None, "application_check_passed": None},
                 {"grpc_status": 0, "application_check_passed": True}),
                ("redis", {"redis_reply": None}, {"redis_reply": "PONG"})):
            policy = deepcopy(self.policy)
            policy["health"]["protocol"] = protocol
            del policy["health"]["required_http_status"]
            policy["health"].update({"grpc": {"required_grpc_status": 0},
                                     "redis": {"required_redis_reply": "PONG"}}[protocol])
            rows = [dict(row, protocol=protocol, **(silent if index == 0 else answered))
                    for index, row in enumerate(live_readings())]
            for row in rows:
                row.pop("http_status")
            with self.subTest(protocol=protocol):
                self.assertEqual(verify(self.execution, rows, policy)["status"], "RECOVERED")
        # A gRPC status without the application check is a partial answer, not silence: it counts.
        policy = deepcopy(self.policy)
        policy["health"].update(protocol="grpc", required_grpc_status=0)
        rows = [dict(row, protocol="grpc", grpc_status=0, application_check_passed=True) for row in live_readings()]
        rows[0]["application_check_passed"] = None
        self.assertEqual(verify(self.execution, rows[1:], policy)["status"], "RECOVERED")
        self.assertEqual(verify(self.execution, rows, policy)["status"], "UNKNOWN")


class RecreateLab(SimLab):
    """SimLab's Coordinator, delivery and remote Verifier, with a target that rolls out by Recreate."""

    def __init__(self, test, *, pod_from=8.0, healthy_from=13.0, answers=200, facts=True):
        super().__init__(test, healthy_from=healthy_from)
        self.pod_from, self.answers, self.facts = pod_from, answers, facts
        deployment = self.f.cluster.read(self.execution["target"])["deployment"]
        self.generation = deployment["metadata"]["generation"]

    def reading(self, _execution):
        after = self.t - self.t0
        pod, healthy = after >= self.pod_from, after >= self.healthy_from
        row = {"at": self.now().isoformat().replace("+00:00", "Z"), "service": self.execution["target"]["name"],
               "target": deepcopy(self.execution["target"]), "protocol": "http",
               "http_status": self.answers if healthy else None, "ready_replicas": int(healthy),
               "desired_replicas": 1, "available_replicas": int(healthy), "dependencies": {},
               "template_sha256": self.template, "configuration_stable": True}
        if self.facts:
            row.update(generation=self.generation, observed_generation=self.generation,
                       updated_replicas=int(pod), total_replicas=int(pod))
        return [row]


class RecreateTimelineTests(unittest.TestCase):
    """The real delivery layer, RemoteWorker and VerifierAdapter under the kind health policy."""

    def run_lab(self, **options):
        lab = RecreateLab(self, **options)
        worker = lab.worker()
        lab.advance(6)  # The first reading lands about 6 s after the write, as in r1.
        return lab, lab.run(worker)

    def test_r1_a_pod_less_first_reading_then_sustained_health_is_recovered(self):
        self.assertEqual(KIND_HEALTH, {key: dist_policy()["health"][key] for key in KIND_HEALTH})
        lab, state = self.run_lab()
        self.assertEqual(state, "RECOVERED")
        statuses = [status for _, status in lab.readings()]
        self.assertEqual(statuses[0], "incomplete")
        self.assertTrue(all(status == "healthy" for status in statuses[1:]), statuses)
        outcome = lab.f.verification.get(lab.action_id)["outcome"]
        self.assertIn(AWAITING_ROLLOUT_CAUSE, convergence(outcome)["detail"])
        # Decided once a 10 s healthy window followed, well inside the 60 s grace.
        self.assertLessEqual(lab.after(), KIND_HEALTH["convergence_grace_seconds"])

    def test_the_same_timeline_without_rollout_counts_is_left_for_review_as_live(self):
        lab, state = self.run_lab(facts=False)
        self.assertEqual(state, "NEEDS_REVIEW")
        self.assertEqual(lab.f.verification.get(lab.action_id)["outcome"]["status"], "UNKNOWN")

    def test_a_pod_that_never_becomes_healthy_is_still_never_recovered(self):
        for answers, healthy_from in ((None, float("inf")), (503, 13.0)):
            with self.subTest(answers=answers):
                lab, state = self.run_lab(answers=answers, healthy_from=healthy_from)
                self.assertEqual(state, "NEEDS_REVIEW")
                self.assertEqual(lab.f.verification.get(lab.action_id)["outcome"]["status"], "UNKNOWN")


class CollectorRolloutCountsTests(unittest.TestCase):
    """The live collector records the rollout counts, bounded integers, from the same Deployment read."""

    def setUp(self):
        self.kubernetes, self.http = Kubernetes(), HTTP()
        self.collector = LiveEvidenceCollector(configuration(), self.kubernetes, http=self.http,
                                               clock=lambda: datetime(2026, 10, 26, tzinfo=UTC))
        self.target = configuration()["services"]["payment"]["target"]

    def test_a_pod_less_recreate_reading_carries_what_the_verifier_needs(self):
        self.kubernetes.resources["payment"]["metadata"]["generation"] = 3
        self.kubernetes.resources["payment"]["status"] = {"observedGeneration": 3, "updatedReplicas": 0}
        self.http.statuses["payment"] = None
        sample = self.collector.collect(self.target)["sample"]
        self.assertEqual({key: sample[key] for key in (*ROLLOUT_FIELDS, "ready_replicas", "available_replicas",
                                                         "http_status")},
                         {"generation": 3, "observed_generation": 3, "updated_replicas": 0, "total_replicas": 0,
                          "ready_replicas": 0, "available_replicas": 0, "http_status": None})
        self.assertTrue(all(type(sample[key]) is int for key in ROLLOUT_FIELDS))

    def test_a_healthy_reading_carries_the_finished_rollout(self):
        self.kubernetes.resources["payment"]["status"]["replicas"] = 1
        sample = self.collector.collect(self.target)["sample"]
        self.assertEqual({key: sample[key] for key in ROLLOUT_FIELDS},
                         {"generation": 2, "observed_generation": 2, "updated_replicas": 1, "total_replicas": 1})

    def test_a_failed_or_inconsistent_deployment_read_records_no_counts(self):
        self.kubernetes.resources["payment"]["status"]["replicas"] = -1
        sample = self.collector.collect(self.target)["sample"]
        self.assertFalse(set(ROLLOUT_FIELDS) & set(sample))
        self.assertIsNone(sample["ready_replicas"])
        self.kubernetes.resources["payment"]["status"]["replicas"] = 1
        self.kubernetes.after_read = lambda value: value["metadata"].update(generation=7)
        self.assertFalse(set(ROLLOUT_FIELDS) & set(self.collector.collect(self.target)["sample"]))


if __name__ == "__main__":
    unittest.main()
