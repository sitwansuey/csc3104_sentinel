import copy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.k8s_observer_adapter import (
    DeploymentObservationMonitor,
    deployment_to_observation,
    observe_deployment,
    read_deployment_observation,
)
from services.observer import Observer, detect, validate_observation
from shared.contracts import validate_message


OBSERVED_AT = "2026-10-10T12:00:00Z"
CLUSTER_ID = "sentinel-lab"


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class FakeAppsApi:
    def __init__(self, deployment):
        self.deployment = deployment
        self.calls = []

    def read_namespaced_deployment(self, *, name, namespace):
        self.calls.append({"name": name, "namespace": namespace})
        return copy.deepcopy(self.deployment)


class SequenceAppsApi:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def read_namespaced_deployment(self, *, name, namespace):
        self.calls.append({"name": name, "namespace": namespace})
        if len(self.responses) > 1:
            response = self.responses.pop(0)
        else:
            response = self.responses[0]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class SequenceClock:
    def __init__(self, timestamps):
        self.timestamps = list(timestamps)

    def __call__(self):
        if len(self.timestamps) > 1:
            return self.timestamps.pop(0)
        return self.timestamps[0]


class KubernetesObserverAdapterTests(unittest.TestCase):
    def setUp(self):
        self.policy = fixture("samples/policy.json")

    def test_healthy_deployment_normalizes_to_healthy_observation(self):
        observation = deployment_to_observation(
            self.healthy_deployment(), cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )

        validate_observation(observation)
        self.assertEqual(observation["source"], "kubernetes_api")
        self.assertEqual(observation["captured_at"], OBSERVED_AT)
        self.assertEqual(observation["pods"], [])
        self.assertEqual(observation["events"], [])
        self.assertEqual(observation["logs"], [])
        self.assertEqual(observation["deployment"]["status"]["readyReplicas"], 1)
        self.assertEqual(observation["deployment"]["status"]["availableReplicas"], 1)

    def test_failed_rollout_normalizes_to_failed_observation(self):
        observation = deployment_to_observation(
            self.failed_deployment(), cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )

        validate_observation(observation)
        self.assertEqual(observation["deployment"]["status"]["readyReplicas"], 0)
        self.assertEqual(observation["deployment"]["status"]["availableReplicas"], 0)
        self.assertEqual(observation["deployment"]["status"]["unavailableReplicas"], 1)
        self.assertEqual(observation["events"][0]["reason"], "ProgressDeadlineExceeded")
        self.assertEqual(observation["events"][0]["type"], "Warning")

    def test_generation_information_is_preserved(self):
        observation = deployment_to_observation(
            self.failed_deployment(generation=7, observed_generation=6),
            cluster_id=CLUSTER_ID,
            observed_at=OBSERVED_AT,
        )

        metadata = observation["deployment"]["metadata"]
        status = observation["deployment"]["status"]
        self.assertEqual(metadata["generation"], 7)
        self.assertEqual(status["observedGeneration"], 6)

    def test_replica_state_is_preserved(self):
        observation = deployment_to_observation(
            self.failed_deployment(
                desired=3, replicas=3, updated=2, ready=1, available=1, unavailable=2
            ),
            cluster_id=CLUSTER_ID,
            observed_at=OBSERVED_AT,
        )

        status = observation["deployment"]["status"]
        self.assertEqual(observation["deployment"]["spec"]["replicas"], 3)
        self.assertEqual(status["replicas"], 3)
        self.assertEqual(status["updatedReplicas"], 2)
        self.assertEqual(status["readyReplicas"], 1)
        self.assertEqual(status["availableReplicas"], 1)
        self.assertEqual(status["unavailableReplicas"], 2)

    def test_rollout_failure_reason_is_preserved(self):
        observations = self.failed_observations()
        incident = None
        observer = Observer(self.policy)
        for observation in observations:
            incident = observer.observe(observation) or incident

        validate_message(incident)
        reasons = {signal["reason"] for signal in incident["signals"]}
        self.assertIn("ProgressDeadlineExceeded", reasons)

    def test_namespace_and_workload_identity_are_preserved(self):
        observation = deployment_to_observation(
            self.healthy_deployment(name="checkout-api", namespace="shop", uid="uid-123"),
            cluster_id="prod-cluster",
            observed_at=OBSERVED_AT,
        )

        metadata = observation["deployment"]["metadata"]
        self.assertEqual(observation["cluster_id"], "prod-cluster")
        self.assertEqual(metadata["name"], "checkout-api")
        self.assertEqual(metadata["namespace"], "shop")
        self.assertEqual(metadata["uid"], "uid-123")

    def test_same_deployment_state_produces_deterministic_observation(self):
        deployment = self.failed_deployment()

        first = deployment_to_observation(
            deployment, cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )
        second = deployment_to_observation(
            copy.deepcopy(deployment), cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )

        self.assertEqual(first, second)

    def test_adapter_output_is_accepted_by_observer(self):
        observation = deployment_to_observation(
            self.failed_deployment(), cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )

        snapshot = validate_observation(observation)
        self.assertEqual(snapshot.target["name"], "demo-api")

    def test_healthy_adapter_output_creates_no_incident(self):
        observation = deployment_to_observation(
            self.healthy_deployment(), cluster_id=CLUSTER_ID, observed_at=OBSERVED_AT
        )

        self.assertIsNone(detect([observation], self.policy))

    def test_sustained_failed_rollout_creates_one_incident(self):
        observer = Observer(self.policy)
        incidents = []
        for observation in self.failed_observations():
            incident = observer.observe(observation)
            if incident is not None:
                incidents.append(incident)

        self.assertEqual(len(incidents), 1)
        validate_message(incidents[0])
        self.assertEqual(incidents[0]["target"], self.policy["targets"][0])

    def test_repeated_identical_live_state_does_not_create_duplicate_incidents(self):
        observer = Observer(self.policy)
        observations = self.failed_observations()
        emitted = []
        for observation in observations:
            incident = observer.observe(observation)
            if incident is not None:
                emitted.append(incident)

        self.assertEqual(len(emitted), 1)
        self.assertIsNone(observer.observe(copy.deepcopy(observations[-1])))

    def test_read_helper_uses_injected_client_without_importing_kubernetes(self):
        api = FakeAppsApi(self.healthy_deployment())

        observation = read_deployment_observation(
            api,
            namespace="sentinel-demo",
            name="demo-api",
            cluster_id=CLUSTER_ID,
            observed_at=OBSERVED_AT,
        )

        self.assertEqual(api.calls, [{"namespace": "sentinel-demo", "name": "demo-api"}])
        self.assertEqual(observation["deployment"]["metadata"]["name"], "demo-api")

    def test_observe_helper_passes_normalized_observation_to_observer(self):
        api = FakeAppsApi(self.failed_deployment())
        observer = Observer(self.policy)

        self.assertIsNone(observe_deployment(
            observer,
            api,
            namespace="sentinel-demo",
            name="demo-api",
            cluster_id=CLUSTER_ID,
            observed_at="2026-10-10T12:00:00Z",
        ))
        self.assertIsNone(observe_deployment(
            observer,
            api,
            namespace="sentinel-demo",
            name="demo-api",
            cluster_id=CLUSTER_ID,
            observed_at="2026-10-10T12:00:15Z",
        ))
        incident = observe_deployment(
            observer,
            api,
            namespace="sentinel-demo",
            name="demo-api",
            cluster_id=CLUSTER_ID,
            observed_at="2026-10-10T12:00:30Z",
        )

        validate_message(incident)

    def test_monitor_generates_observation_from_api_client(self):
        observations = []
        monitor = self.monitor(
            [self.healthy_deployment()],
            ["2026-10-10T12:00:00Z"],
            on_observation=observations.append,
        )

        result = monitor.run(max_iterations=1)

        self.assertEqual(result["observations"], 1)
        self.assertEqual(result["incidents"], [])
        self.assertEqual(observations[0]["deployment"]["metadata"]["name"], "demo-api")

    def test_monitor_handles_transient_api_failure_and_resumes(self):
        errors = []
        sleeps = []
        monitor = self.monitor(
            [RuntimeError("temporary API outage"), self.healthy_deployment()],
            ["2026-10-10T12:00:00Z", "2026-10-10T12:00:15Z"],
            interval_seconds=1,
            on_error=errors.append,
            sleep=sleeps.append,
        )

        result = monitor.run(max_iterations=2)

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["observations"], 1)
        self.assertEqual(result["incidents"], [])
        self.assertEqual(str(errors[0]), "temporary API outage")
        self.assertEqual(sleeps, [1.0])

    def test_monitor_repeated_deployment_state_does_not_flood_incidents(self):
        monitor = self.monitor(
            [self.failed_deployment()] * 4,
            [
                "2026-10-10T12:00:00Z",
                "2026-10-10T12:00:15Z",
                "2026-10-10T12:00:30Z",
                "2026-10-10T12:00:45Z",
            ],
        )

        result = monitor.run(max_iterations=4)

        self.assertEqual(len(result["incidents"]), 1)
        validate_message(result["incidents"][0])

    def test_monitor_healthy_then_sustained_failure_creates_one_incident(self):
        monitor = self.monitor(
            [
                self.healthy_deployment(),
                self.failed_deployment(),
                self.failed_deployment(),
                self.failed_deployment(),
            ],
            [
                "2026-10-10T12:00:00Z",
                "2026-10-10T12:00:15Z",
                "2026-10-10T12:00:30Z",
                "2026-10-10T12:00:45Z",
            ],
        )

        result = monitor.run(max_iterations=4)

        self.assertEqual(len(result["incidents"]), 1)
        validate_message(result["incidents"][0])

    def test_monitor_failure_then_healthy_resets_episode(self):
        monitor = self.monitor(
            [
                self.failed_deployment(),
                self.failed_deployment(),
                self.healthy_deployment(),
                self.failed_deployment(),
            ],
            [
                "2026-10-10T12:00:00Z",
                "2026-10-10T12:00:15Z",
                "2026-10-10T12:00:30Z",
                "2026-10-10T12:00:45Z",
            ],
        )

        result = monitor.run(max_iterations=4)

        self.assertEqual(result["incidents"], [])

    def test_monitor_later_new_failure_can_generate_new_incident(self):
        monitor = self.monitor(
            [
                self.failed_deployment(),
                self.failed_deployment(),
                self.failed_deployment(),
                self.healthy_deployment(),
                self.failed_deployment(generation=3, observed_generation=3),
                self.failed_deployment(generation=3, observed_generation=3),
                self.failed_deployment(generation=3, observed_generation=3),
            ],
            [
                "2026-10-10T12:00:00Z",
                "2026-10-10T12:00:15Z",
                "2026-10-10T12:00:30Z",
                "2026-10-10T12:00:45Z",
                "2026-10-10T12:01:00Z",
                "2026-10-10T12:01:15Z",
                "2026-10-10T12:01:30Z",
            ],
        )

        result = monitor.run(max_iterations=7)

        self.assertEqual(len(result["incidents"]), 2)
        self.assertNotEqual(
            result["incidents"][0]["incident_id"],
            result["incidents"][1]["incident_id"],
        )

    def test_monitor_malformed_kubernetes_response_is_reported(self):
        malformed = self.failed_deployment()
        del malformed["metadata"]["uid"]
        errors = []
        monitor = self.monitor(
            [malformed],
            ["2026-10-10T12:00:00Z"],
            max_consecutive_errors=0,
            on_error=errors.append,
        )

        result = monitor.run(max_iterations=1)

        self.assertEqual(result["errors"], 1)
        self.assertEqual(result["observations"], 0)
        self.assertEqual(result["incidents"], [])
        self.assertIn("deployment.metadata.uid", str(errors[0]))

    def test_monitor_can_stop_cleanly(self):
        holder = {}
        sleeps = []

        def stop_after_observation(_observation):
            holder["monitor"].stop()

        monitor = self.monitor(
            [self.healthy_deployment()] * 5,
            ["2026-10-10T12:00:00Z"],
            on_observation=stop_after_observation,
            sleep=sleeps.append,
        )
        holder["monitor"] = monitor

        result = monitor.run(max_iterations=5)

        self.assertEqual(result["observations"], 1)
        self.assertTrue(result["stopped"])
        self.assertEqual(sleeps, [])

    def failed_observations(self):
        observations = []
        for timestamp in (
            "2026-10-10T12:00:00Z",
            "2026-10-10T12:00:15Z",
            "2026-10-10T12:00:30Z",
        ):
            observations.append(deployment_to_observation(
                self.failed_deployment(),
                cluster_id=CLUSTER_ID,
                observed_at=timestamp,
            ))
        return observations

    def monitor(
        self,
        responses,
        timestamps,
        *,
        interval_seconds=0,
        max_consecutive_errors=3,
        on_observation=None,
        on_error=None,
        sleep=None,
    ):
        return DeploymentObservationMonitor(
            Observer(self.policy),
            SequenceAppsApi(responses),
            namespace="sentinel-demo",
            name="demo-api",
            cluster_id=CLUSTER_ID,
            interval_seconds=interval_seconds,
            max_consecutive_errors=max_consecutive_errors,
            clock=SequenceClock(timestamps),
            sleep=sleep or (lambda _seconds: None),
            on_observation=on_observation,
            on_error=on_error,
        )

    def healthy_deployment(self, *, name="demo-api", namespace="sentinel-demo", uid="synthetic-demo-uid-001"):
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "uid": uid,
                "resourceVersion": "101",
                "generation": 1,
                "labels": {"app": name},
            },
            "spec": {
                "replicas": 1,
            },
            "status": {
                "observedGeneration": 1,
                "replicas": 1,
                "updatedReplicas": 1,
                "readyReplicas": 1,
                "availableReplicas": 1,
                "unavailableReplicas": 0,
                "conditions": [
                    {
                        "type": "Progressing",
                        "status": "True",
                        "reason": "NewReplicaSetAvailable",
                        "message": "Deployment has minimum availability.",
                        "lastTransitionTime": "2026-10-10T11:59:55Z",
                    },
                    {
                        "type": "Available",
                        "status": "True",
                        "reason": "MinimumReplicasAvailable",
                        "message": "Deployment has minimum availability.",
                        "lastTransitionTime": "2026-10-10T11:59:55Z",
                    },
                ],
            },
        }

    def failed_deployment(
        self,
        *,
        generation=2,
        observed_generation=2,
        desired=1,
        replicas=1,
        updated=1,
        ready=0,
        available=0,
        unavailable=1,
    ):
        deployment = self.healthy_deployment()
        deployment["metadata"]["resourceVersion"] = "102"
        deployment["metadata"]["generation"] = generation
        deployment["spec"]["replicas"] = desired
        deployment["status"] = {
            "observedGeneration": observed_generation,
            "replicas": replicas,
            "updatedReplicas": updated,
            "readyReplicas": ready,
            "availableReplicas": available,
            "unavailableReplicas": unavailable,
            "conditions": [
                {
                    "type": "Available",
                    "status": "False",
                    "reason": "MinimumReplicasUnavailable",
                    "message": "Deployment does not have minimum availability.",
                    "lastTransitionTime": "2026-10-10T12:00:00Z",
                },
                {
                    "type": "Progressing",
                    "status": "False",
                    "reason": "ProgressDeadlineExceeded",
                    "message": "ReplicaSet demo-api-bad has timed out progressing.",
                    "lastTransitionTime": "2026-10-10T12:00:00Z",
                },
            ],
        }
        return deployment


if __name__ == "__main__":
    unittest.main()
