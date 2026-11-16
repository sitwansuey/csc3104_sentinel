import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.coordinator import core as coordinator
from services.observer.core import Observer, ObservationError, detect, detect_all
from shared.contracts import validate_message


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class ObserverDetectionTests(unittest.TestCase):
    def setUp(self):
        self.observations = fixture("data/sentinel/samples/observations.json")
        self.policy = fixture("data/sentinel/samples/policy.json")

    def test_healthy_observations_do_not_create_incident(self):
        self.assertEqual(detect_all(self.observations["healthy"], self.policy), [])
        self.assertIsNone(detect(self.observations["healthy"], self.policy))

    def test_single_transient_failure_is_suppressed(self):
        self.assertEqual(detect_all(self.observations["transient-failure"], self.policy), [])

    def test_sustained_failure_creates_one_contract_valid_incident(self):
        incidents = detect_all(self.observations["failed-rollout"], self.policy)

        self.assertEqual(len(incidents), 1)
        incident = incidents[0]
        validate_message(incident)
        self.assertEqual(incident["kind"], "incident")
        self.assertEqual(incident["target"], self.policy["targets"][0])
        self.assertEqual(incident["observed_at"], "2026-10-10T12:00:30Z")
        self.assertEqual(incident["observed_resource_version"], "102")
        self.assertEqual(incident["observed_generation"], 2)
        self.assertIn("CrashLoopBackOff", {signal["reason"] for signal in incident["signals"]})

    def test_overlapping_tails_are_one_signal_per_occurrence_and_emitting_container(self):
        observations = copy.deepcopy(self.observations["failed-rollout"])
        for observation in observations:
            log = observation["logs"][0]
            log["text"] = "2026-10-10T12:00:00Z Error startup failed\nError without a timestamp"
            observation["logs"].append({**log, "pod": "another-pod"})
        incident = detect(observations, self.policy)
        timed = [s for s in incident["signals"] if s["source"] == "container_log"]
        untimed = [s for s in incident["signals"] if s["source"] == "container_log_untimed"]
        self.assertEqual(len(timed), 2)
        self.assertEqual(len(untimed), 2)
        self.assertTrue(all(s["at"] == "2026-10-10T12:00:00Z" for s in timed))

    def test_exact_duplicate_observation_does_not_create_duplicate_incident(self):
        observer = Observer(self.policy)
        emitted = []
        for observation in self.observations["failed-rollout"]:
            incident = observer.observe(observation)
            if incident is not None:
                emitted.append(incident)

        self.assertEqual(len(emitted), 1)
        self.assertIsNone(observer.observe(copy.deepcopy(self.observations["failed-rollout"][-1])))

    def test_repeated_samples_from_same_failure_do_not_flood_incidents(self):
        observer = Observer(self.policy)
        emitted = []
        for observation in self.observations["failed-rollout"]:
            incident = observer.observe(observation)
            if incident is not None:
                emitted.append(incident)

        repeated = copy.deepcopy(self.observations["failed-rollout"][-1])
        repeated["captured_at"] = "2026-10-10T12:00:45Z"
        repeated["events"][0]["lastTimestamp"] = "2026-10-10T12:00:45Z"
        repeated["logs"][0]["at"] = "2026-10-10T12:00:45Z"
        repeated["pods"][0]["status"]["containerStatuses"][0]["restartCount"] = 5

        self.assertIsNone(observer.observe(repeated))
        self.assertEqual(len(emitted), 1)

    def test_different_failure_after_recovery_creates_new_incident(self):
        observer = Observer(self.policy)
        first_incident = None
        for observation in self.observations["failed-rollout"]:
            first_incident = observer.observe(observation) or first_incident
        self.assertIsNotNone(first_incident)

        recovered = copy.deepcopy(self.observations["healthy"][-1])
        recovered["captured_at"] = "2026-10-10T12:01:00Z"
        recovered["deployment"]["metadata"]["resourceVersion"] = "103"
        self.assertIsNone(observer.observe(recovered))

        second_failure = self._failure_sequence("2026-10-10T12:01", generation=3, resource_version="104")
        second_incident = None
        for observation in second_failure:
            second_incident = observer.observe(observation) or second_incident

        self.assertIsNotNone(second_incident)
        validate_message(second_incident)
        self.assertNotEqual(first_incident["incident_id"], second_incident["incident_id"])
        self.assertEqual(second_incident["observed_generation"], 3)

    def test_evolving_kubernetes_reasons_keep_one_failure_timer_and_incident(self):
        # A real failed rollout gains Available=False, then ProgressDeadlineExceeded.
        # Those new symptoms must neither restart the timer nor emit a duplicate.
        observer = Observer(self.policy)
        start = datetime(2026, 10, 25, tzinfo=timezone.utc)
        incidents = []
        for seconds in range(0, 121, 15):
            observation = copy.deepcopy(self.observations["failed-rollout"][0])
            at = (start + timedelta(seconds=seconds)).isoformat()
            observation.update(captured_at=at, pods=[], logs=[], events=[])
            if seconds >= 15:
                reason = "ProgressDeadlineExceeded" if seconds >= 90 else "MinimumReplicasUnavailable"
                observation["events"] = [{
                    "reason": reason, "type": "Warning", "message": reason,
                    "lastTimestamp": at,
                }]
            incident = observer.observe(observation)
            if incident:
                incidents.append(incident)
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["observed_at"], "2026-10-25T00:00:30Z")

    def test_new_generation_starts_an_episode_without_intervening_recovery(self):
        observer = Observer(self.policy)
        first = [observer.observe(item) for item in self.observations["failed-rollout"]]
        second = [observer.observe(item) for item in self._failure_sequence(
            "2026-10-10T12:02", generation=3, resource_version="200",
        )]
        self.assertEqual(sum(item is not None for item in first), 1)
        self.assertEqual(sum(item is not None for item in second), 1)
        self.assertNotEqual(first[-1]["incident_id"], second[-1]["incident_id"])

    def test_recovery_resets_suppression_within_the_same_generation(self):
        observer = Observer(self.policy)
        for item in self.observations["failed-rollout"]:
            observer.observe(item)
        healthy = copy.deepcopy(self.observations["healthy"][-1])
        healthy["captured_at"] = "2026-10-10T12:01:00Z"
        healthy["deployment"]["metadata"]["generation"] = 2
        healthy["deployment"]["status"]["observedGeneration"] = 2
        self.assertIsNone(observer.observe(healthy))
        incidents = [observer.observe(item) for item in self._failure_sequence(
            "2026-10-10T12:02", generation=2, resource_version="200",
        )]
        self.assertEqual(sum(item is not None for item in incidents), 1)

    def test_malformed_observation_fails_predictably(self):
        malformed = copy.deepcopy(self.observations["failed-rollout"][0])
        del malformed["deployment"]["metadata"]["uid"]

        with self.assertRaises(ObservationError):
            detect([malformed], self.policy)

    def test_generated_incident_is_coordinator_compatible_input(self):
        incident = detect(self.observations["failed-rollout"], self.policy)
        validate_message(incident)

        with coordinator.SQLiteStore() as store:
            bus = coordinator.LocalEventBus()
            coordinator.handle(incident, store, bus)
            self.assertEqual(store.incident(incident["incident_id"])["state"], "OPEN")
            self.assertEqual(bus.events[0]["payload"]["incident"], incident)

    def _failure_sequence(self, minute_prefix, *, generation, resource_version):
        sequence = []
        for index, observation in enumerate(self.observations["failed-rollout"]):
            clone = copy.deepcopy(observation)
            timestamp = f"{minute_prefix}:{index * 15:02d}Z"
            clone["captured_at"] = timestamp
            clone["deployment"]["metadata"]["generation"] = generation
            clone["deployment"]["metadata"]["resourceVersion"] = resource_version
            clone["deployment"]["status"]["observedGeneration"] = generation
            clone["events"][0]["lastTimestamp"] = timestamp
            clone["logs"][0]["at"] = timestamp
            sequence.append(clone)
        return sequence


class CertificateRejectedTests(unittest.TestCase):
    """P5.06: TLS certificate refusals in collected logs become CertificateRejected; ordinary logs do not."""

    # Envoy access-log lines (scripts/security/security_lab.py upstream_tls) and Envoy/BoringSSL log
    # lines for an expired certificate, an unknown CA and a SAN mismatch.
    REJECTIONS = (
        '{"route":"shipping-rpc","status":"503","flags":"UF","upstream_tls":"TLS_error:|268435581:SSL '
        'routines:OPENSSL_internal:CERTIFICATE_VERIFY_FAILED:TLS_error_end"}',
        "[warning][connection] TLS error: verify cert failed: X509_verify_cert: certificate verification error at "
        "depth 0: certificate has expired",
        "TLS_error:|268436502:SSL routines:OPENSSL_internal:TLSV1_ALERT_UNKNOWN_CA:TLS_error_end",
        "remote error: tls: unknown ca",
        "verify cert failed: SAN matcher",
        "2026-10-10T12:00:00Z SSLV3_ALERT_CERTIFICATE_EXPIRED",
        "x509: certificate signed by unknown authority (possibly because of unable to get local issuer certificate)",
    )
    ORDINARY = (
        '{"route":"shipping-rpc","status":"200","flags":"-","peer_serial":"1A2B","upstream_tls":"-"}',
        "Invalid startup configuration; process exited with code 1",
        "loaded certificate chain from /pki/upstream/tls.crt",
        "certificate rotated; serial 1A2B",
    )

    def setUp(self):
        self.observations = fixture("data/sentinel/samples/observations.json")
        self.policy = fixture("data/sentinel/samples/policy.json")

    def incident(self, text):
        observations = copy.deepcopy(self.observations["failed-rollout"])
        for observation in observations:
            observation["logs"][0]["text"] = text
        incident = detect(observations, self.policy)
        validate_message(incident)
        return {signal["reason"] for signal in incident["signals"] if signal["source"].startswith("container_log")}

    def test_tls_certificate_refusals_are_certificate_rejected(self):
        for line in self.REJECTIONS:
            with self.subTest(line=line):
                self.assertEqual(self.incident(line), {"CertificateRejected"})

    def test_ordinary_logs_and_mentions_of_a_certificate_are_not(self):
        for line in self.ORDINARY:
            with self.subTest(line=line):
                self.assertNotIn("CertificateRejected", self.incident(line))
        self.assertEqual(self.incident(self.ORDINARY[1]), {"StartupError"})

    def test_a_healthy_deployment_with_a_refusal_in_its_logs_is_still_no_incident(self):
        # The classification labels evidence; it does not by itself create an incident (docs/security.md).
        observations = copy.deepcopy(self.observations["healthy"])
        for observation in observations:
            observation["logs"] = [{"pod": "demo-api-fixture", "container": "envoy", "text": self.REJECTIONS[0],
                                    "at": observation["captured_at"]}]
        self.assertEqual(detect_all(observations, self.policy), [])


if __name__ == "__main__":
    unittest.main()
