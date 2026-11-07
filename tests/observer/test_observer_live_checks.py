import copy
from datetime import datetime, timedelta, timezone
import unittest

from scripts.observer.observer_live_checks import assess_report


def report(*, healthy=False, count=None):
    count = count or (5 if healthy else 9)
    start = datetime(2026, 10, 25, tzinfo=timezone.utc)
    polls = []
    for i in range(count):
        polls.append({
            "captured_at": (start + timedelta(seconds=15 * i)).isoformat(),
            "uid": "live-deployment-uid", "generation": 2,
            "observedGeneration": 2, "desiredReplicas": 1,
            "readyReplicas": int(healthy), "availableReplicas": int(healthy),
        })
    incidents = [] if healthy else [{"incident_id": "INC-TEST", "observed_at": polls[2]["captured_at"]}]
    return {
        "polls": polls, "poll_count": count, "observations_read": count,
        "errors": [], "incident_count": len(incidents), "incidents": incidents,
        "emissions": [] if healthy else [{"poll_index": 2, "incident_id": "INC-TEST"}],
        "failure_duration_seconds": 30,
    }


class LiveAcceptanceTests(unittest.TestCase):
    def test_complete_healthy_window_passes(self):
        self.assertTrue(assess_report(report(healthy=True), "healthy")["passed"])

    def test_45_seconds_is_insufficient_for_healthy_acceptance(self):
        self.assertFalse(assess_report(report(healthy=True, count=4), "healthy")["passed"])

    def test_unavailable_desired_replica_fails_healthy_check(self):
        evidence = report(healthy=True)
        evidence["polls"][2]["availableReplicas"] = 0
        self.assertFalse(assess_report(evidence, "healthy")["passed"])

    def test_exact_threshold_and_duplicate_window_pass(self):
        result = assess_report(report(), "persistent-failure")
        self.assertTrue(result["passed"])
        self.assertEqual(result["detection_delay_seconds"], 30)
        self.assertEqual(result["duplicate_observation_seconds"], 90)

    def test_threshold_uses_initial_detection_after_a_healthy_poll(self):
        evidence = report()
        evidence["polls"][0].update(readyReplicas=1, availableReplicas=1)
        evidence["emissions"][0]["poll_index"] = 3
        evidence["incidents"][0]["observed_at"] = evidence["polls"][3]["captured_at"]
        self.assertTrue(assess_report(evidence, "persistent-failure")["passed"])

    def test_first_poll_after_non_multiple_threshold_passes(self):
        evidence = report()
        evidence["failure_duration_seconds"] = 29
        self.assertTrue(assess_report(evidence, "persistent-failure")["passed"])

    def test_early_and_late_incidents_fail(self):
        for index in (1, 3):
            with self.subTest(index=index):
                evidence = report()
                evidence["emissions"][0]["poll_index"] = index
                evidence["incidents"][0]["observed_at"] = evidence["polls"][index]["captured_at"]
                self.assertFalse(assess_report(evidence, "persistent-failure")["passed"])

    def test_duplicate_incident_fails(self):
        evidence = report()
        evidence["incidents"].append(copy.deepcopy(evidence["incidents"][0]))
        evidence["emissions"].append({"poll_index": 8, "incident_id": "INC-DUP"})
        evidence["incident_count"] = 2
        self.assertFalse(assess_report(evidence, "persistent-failure")["passed"])

    def test_45_seconds_after_incident_is_insufficient(self):
        self.assertFalse(assess_report(report(count=6), "persistent-failure")["passed"])

    def test_collection_errors_and_missing_polls_fail(self):
        for change in ({"errors": ["API unavailable"]}, {"observations_read": 8}):
            with self.subTest(change=change):
                evidence = report()
                evidence.update(change)
                self.assertFalse(assess_report(evidence, "persistent-failure")["passed"])

    def test_recovery_during_failure_window_fails(self):
        evidence = report()
        evidence["polls"][5].update(readyReplicas=1, availableReplicas=1)
        self.assertFalse(assess_report(evidence, "persistent-failure")["passed"])

    def test_deployment_replacement_or_generation_change_fails(self):
        for field, value in (("uid", "replacement"), ("generation", 3)):
            with self.subTest(field=field):
                evidence = report()
                evidence["polls"][-1][field] = value
                self.assertFalse(assess_report(evidence, "persistent-failure")["passed"])


if __name__ == "__main__":
    unittest.main()
