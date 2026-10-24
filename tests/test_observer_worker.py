from contextlib import redirect_stdout, redirect_stderr, closing
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore, is_ready, status_file
from services.observer import Observer, ObservationError
from services import observer_cli

ROOT = Path(__file__).resolve().parents[1]
POLICY = {"detection": {"failure_duration_seconds": 30, "max_sample_gap_seconds": 45}}
TARGET = {"cluster_id": "sentinel-lab", "namespace": "sentinel-demo", "kind": "Deployment", "name": "demo-api"}
START = datetime(2026, 10, 25, tzinfo=timezone.utc)


def sample(seconds, *, healthy=False, generation=2):
    fixtures = json.loads((ROOT / "samples/observations.json").read_text(encoding="utf-8"))
    observation = copy.deepcopy(fixtures["healthy" if healthy else "failed-rollout"][0])
    observation["captured_at"] = (START + timedelta(seconds=seconds)).isoformat()
    observation["deployment"]["metadata"]["generation"] = generation
    observation["deployment"]["status"]["observedGeneration"] = generation
    observation["collection_warnings"] = []
    return observation


class ObserverCheckpointTests(unittest.TestCase):
    def test_restart_keeps_original_threshold_and_emitted_suppression(self):
        first = Observer(POLICY)
        self.assertIsNone(first.observe(sample(0)))
        second = Observer.from_state(POLICY, first.export_state())
        self.assertIsNone(second.observe(sample(15)))
        incident = second.observe(sample(30))
        self.assertIsNotNone(incident)
        third = Observer.from_state(POLICY, second.export_state())
        self.assertIsNone(third.observe(sample(90)))
        self.assertEqual(incident["observed_at"], "2026-10-25T00:00:30Z")

    def test_stale_healthy_sample_cannot_reset_failure(self):
        observer = Observer(POLICY)
        observer.observe(sample(0))
        observer.observe(sample(15))
        self.assertIsNone(observer.observe(sample(5, healthy=True)))
        self.assertIsNotNone(observer.observe(sample(30)))

    def test_unknown_gap_restarts_unemitted_threshold(self):
        observer = Observer(POLICY)
        observer.observe(sample(0))
        observer.collection_gap()
        self.assertIsNone(observer.observe(sample(30)))
        self.assertIsNone(observer.observe(sample(45)))
        self.assertIsNotNone(observer.observe(sample(60)))

    def test_long_poll_gap_does_not_imply_continuous_failure(self):
        observer = Observer(POLICY)
        observer.observe(sample(0))
        self.assertIsNone(observer.observe(sample(60)))
        self.assertIsNotNone(observer.observe(sample(90)))

    def test_gap_after_emission_does_not_create_duplicate(self):
        observer = Observer(POLICY)
        observer.observe(sample(0))
        observer.observe(sample(30))
        observer.collection_gap()
        self.assertIsNone(observer.observe(sample(300)))

    def test_checkpoint_policy_change_fails(self):
        state = Observer(POLICY).export_state()
        with self.assertRaises(ObservationError):
            Observer.from_state({"detection": {"failure_duration_seconds": 15}}, state)

    def test_recreated_uid_does_not_grow_state_forever(self):
        observer = Observer(POLICY)
        for seconds in range(100):
            item = sample(seconds)
            item["deployment"]["metadata"]["uid"] = f"replacement-{seconds}"
            observer.observe(item)
        self.assertEqual(len(observer.export_state()["latest"]), 1)
        self.assertEqual(len(observer.export_state()["episodes"]), 1)

    def test_initial_evidence_is_retained_when_latest_symptoms_change(self):
        observer = Observer(POLICY)
        observer.observe(sample(0))
        incident = observer.observe(sample(30))
        self.assertIn("2026-10-25T00:00:00Z", {signal["at"] for signal in incident["signals"]})
        self.assertIn("2026-10-25T00:00:30Z", {signal["at"] for signal in incident["signals"]})

    def test_failed_init_container_is_detected_but_successful_init_is_ignored(self):
        for code, expected in ((1, True), (0, False)):
            with self.subTest(code=code):
                policy = {"detection": {"failure_duration_seconds": 0}}
                item = sample(0, healthy=True)
                item["pods"][0]["status"]["initContainerStatuses"] = [{
                    "name": "initialize", "ready": False, "restartCount": 1,
                    "state": {"terminated": {"exitCode": code, "reason": "Error" if code else "Completed"}},
                    "lastState": {},
                }]
                self.assertEqual(Observer(policy).observe(item) is not None, expected)


class DurableWorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name) / "observer.sqlite3"
        self.coordinator = Path(self.directory.name) / "coordinator.sqlite3"

    def emit(self):
        with ObserverStore(self.state, POLICY, TARGET) as store:
            store.observe(sample(0))
            return store.observe(sample(30))

    def test_checkpoint_and_outbox_survive_reopening(self):
        incident = self.emit()
        with ObserverStore(self.state, POLICY, TARGET) as store:
            self.assertEqual(store.pending(), [incident])
            self.assertIsNone(store.observe(sample(90)))
            self.assertEqual(store.status()["incidents"], 1)

    def test_delivery_failure_retries_identical_incident_after_restart(self):
        incident = self.emit()
        def broken(message):
            raise OSError("Coordinator unavailable")
        with ObserverStore(self.state, POLICY, TARGET) as store:
            self.assertEqual(store.flush(broken)["error"], "OSError")
            self.assertEqual(store.pending(), [incident])
        seen = []
        with ObserverStore(self.state, POLICY, TARGET) as store:
            self.assertEqual(store.flush(seen.append), {"delivered": 1, "error": None})
            self.assertEqual(store.pending(), [])
        self.assertEqual(seen, [incident])

    def test_crash_after_coordinator_acceptance_does_not_duplicate_planner_request(self):
        incident = self.emit()
        sink = LocalCoordinatorSink(self.coordinator)
        sink(incident)  # Simulate crash before the Observer acknowledges its own outbox.
        with ObserverStore(self.state, POLICY, TARGET) as store:
            self.assertEqual(store.flush(sink)["delivered"], 1)
        with closing(sqlite3.connect(self.coordinator)) as coordinator:
            self.assertEqual(coordinator.execute("SELECT count(*) FROM incidents").fetchone()[0], 1)
            self.assertEqual(coordinator.execute("SELECT count(*) FROM outbox WHERE topic='planner.request'").fetchone()[0], 1)

    def test_atomic_rollback_does_not_lose_incident(self):
        with ObserverStore(self.state, POLICY, TARGET) as store:
            store.observe(sample(0))
            with patch.object(store, "_save", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    store.observe(sample(30))
            self.assertEqual(store.pending(), [])
        with ObserverStore(self.state, POLICY, TARGET) as store:
            self.assertIsNotNone(store.observe(sample(30)))
            self.assertEqual(len(store.pending()), 1)

    def test_wrong_policy_or_target_cannot_reuse_state(self):
        with ObserverStore(self.state, POLICY, TARGET):
            pass
        for policy, target in (({"detection": {"failure_duration_seconds": 20}}, TARGET),
                               (POLICY, {**TARGET, "name": "another-deployment"})):
            with self.subTest(policy=policy, target=target), self.assertRaises(ValueError):
                ObserverStore(self.state, policy, target)

    def test_wrong_observation_target_is_rejected(self):
        with ObserverStore(self.state, POLICY, TARGET) as store:
            item = sample(0)
            item["cluster_id"] = "wrong-cluster"
            with self.assertRaises(ValueError):
                store.observe(item)
            self.assertEqual(store.status()["polls"], 0)

    def test_collection_error_cannot_look_ready(self):
        with ObserverStore(self.state, POLICY, TARGET) as store:
            store.observe(sample(0, healthy=True))
            self.assertTrue(is_ready(store.status(), now=START + timedelta(seconds=1)))
            self.assertFalse(is_ready(store.status(), now=START + timedelta(seconds=46)))
            store.collection_failed(OSError("no API"))
            self.assertFalse(is_ready(store.status(), now=START + timedelta(seconds=1)))
        self.assertEqual(status_file(self.state)["collection_errors"], 1)

    def test_two_store_connections_serialize_detection(self):
        with ObserverStore(self.state, POLICY, TARGET) as first, ObserverStore(self.state, POLICY, TARGET) as second:
            first.observe(sample(0))
            self.assertIsNotNone(second.observe(sample(30)))
            self.assertIsNone(first.observe(sample(30)))
            self.assertEqual(first.status()["incidents"], 1)

    def test_cli_failure_is_nonzero_and_preserves_state(self):
        collector = type("BrokenCollector", (), {"target": TARGET,
                         "collect": lambda self: (_ for _ in ()).throw(OSError("API down"))})()
        output = io.StringIO()
        with patch.object(observer_cli, "KubectlReader"), patch.object(observer_cli, "KubernetesEvidenceCollector", return_value=collector), redirect_stdout(output):
            code = observer_cli.main(["run", "--context", "lab", "--namespace", "sentinel-demo",
                                      "--deployment", "demo-api", "--cluster-id", "sentinel-lab",
                                      "--state", str(self.state), "--coordinator-db", str(self.coordinator),
                                      "--policy", str(ROOT / "samples/policy.json"), "--poll-count", "1"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["kind"], "observer.collection_error")
        self.assertEqual(status_file(self.state)["collection_errors"], 1)

    def test_invalid_policy_shapes_fail_cleanly(self):
        for policy in ([], {"detection": "invalid"}, {"detection": []}, {"detection": 0}):
            with self.subTest(policy=policy):
                policy_path = Path(self.directory.name) / "policy.json"
                policy_path.write_text(json.dumps(policy))
                errors = io.StringIO()
                with redirect_stderr(errors):
                    code = observer_cli.main(["run", "--context", "lab", "--namespace", "sentinel-demo",
                                              "--deployment", "demo-api", "--cluster-id", "sentinel-lab",
                                              "--state", str(self.state), "--coordinator-db", str(self.coordinator),
                                              "--policy", str(policy_path), "--poll-count", "1"])
                self.assertEqual(code, 1)
                self.assertIn("policy", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
