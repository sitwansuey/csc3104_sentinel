import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from services.observer_kubernetes import CollectionError, InClusterReader, KubectlReader, KubernetesEvidenceCollector, NoRedirect, selector_text


NOW = datetime(2026, 10, 25, tzinfo=timezone.utc)


def owner(kind, uid):
    return {"kind": kind, "uid": uid, "controller": True}


class FakeReader:
    def __init__(self):
        self.deployment = {"metadata": {"name": "demo-api", "namespace": "sentinel-demo", "uid": "dep-1",
                            "resourceVersion": "100", "generation": 2,
                            "annotations": {"deployment.kubernetes.io/revision": "2"}},
                           "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "demo-api"}}},
                           "status": {"observedGeneration": 2, "readyReplicas": 0, "availableReplicas": 0}}
        self.replica_sets = [{"metadata": {"uid": "rs-2", "annotations": {"deployment.kubernetes.io/revision": "2"},
                                          "ownerReferences": [owner("Deployment", "dep-1")]}}]
        self.pods = [{"metadata": {"name": "demo-pod", "uid": "pod-2", "ownerReferences": [owner("ReplicaSet", "rs-2")]},
                      "status": {"phase": "Running", "containerStatuses": [{"name": "demo-api", "ready": False,
                                 "restartCount": 2, "state": {"waiting": {"reason": "CrashLoopBackOff"}}, "lastState": {}}]}}]
        self.events = [{"involvedObject": {"uid": "pod-2"}, "type": "Warning", "reason": "BackOff",
                        "message": "Container startup failed", "lastTimestamp": NOW.isoformat()}]
        self.calls = []
        self.log_error = False
        self.changed = False
        self.deployment_reads = 0

    def get(self, path, parameters=None, *, text=False):
        self.calls.append((path, parameters, text))
        if "/deployments/" in path:
            self.deployment_reads += 1
            result = copy.deepcopy(self.deployment)
            if self.changed and self.deployment_reads > 1:
                result["metadata"]["generation"] += 1
            return result
        if path.endswith("/replicasets"):
            return {"items": copy.deepcopy(self.replica_sets)}
        if path.endswith("/pods"):
            return {"items": copy.deepcopy(self.pods)}
        if path.endswith("/events"):
            return {"items": copy.deepcopy(self.events)}
        if path.endswith("/log"):
            if self.log_error:
                raise CollectionError("Previous log disappeared")
            return '2026-10-25T00:00:00Z {"event":"startup_failed"}'
        raise AssertionError(path)


class EvidenceCollectorTests(unittest.TestCase):
    def collect(self, reader, **kwargs):
        return KubernetesEvidenceCollector(reader, namespace="sentinel-demo", name="demo-api",
                                             cluster_id="lab", clock=lambda: NOW, **kwargs).collect()

    def test_real_owned_pod_warning_and_log_form_one_observation(self):
        reader = FakeReader()
        observation = self.collect(reader, collect_logs=True)
        self.assertEqual(observation["pods"][0]["metadata"]["uid"], "pod-2")
        self.assertIn("BackOff", {event["reason"] for event in observation["events"]})
        self.assertIn("startup_failed", observation["logs"][0]["text"])
        log_parameters = next(params for path, params, _ in reader.calls if path.endswith("/log"))
        self.assertEqual(log_parameters["previous"], "true")
        self.assertEqual(log_parameters["limitBytes"], 4096)

    def test_same_label_unowned_pods_and_events_are_excluded(self):
        reader = FakeReader()
        reader.pods.append({"metadata": {"name": "unrelated", "uid": "unrelated", "ownerReferences": [owner("ReplicaSet", "foreign")]}})
        reader.events.append({"involvedObject": {"uid": "foreign"}, "type": "Warning", "reason": "Unrelated", "lastTimestamp": NOW.isoformat()})
        observation = self.collect(reader)
        self.assertEqual(len(observation["pods"]), 1)
        self.assertNotIn("Unrelated", {event["reason"] for event in observation["events"]})

    def test_old_rollout_pods_do_not_poison_recovered_health(self):
        reader = FakeReader()
        reader.replica_sets[0]["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "1"
        observation = self.collect(reader)
        self.assertEqual(observation["pods"], [])
        self.assertEqual(observation["events"], [])

    def test_old_events_and_terminating_pods_are_excluded(self):
        reader = FakeReader()
        reader.events[0]["lastTimestamp"] = (NOW - timedelta(seconds=301)).isoformat()
        reader.pods[0]["metadata"]["deletionTimestamp"] = NOW.isoformat()
        observation = self.collect(reader)
        self.assertEqual(observation["pods"], [])
        self.assertEqual(observation["events"], [])

    def test_identity_change_during_collection_rejects_sample(self):
        reader = FakeReader()
        reader.changed = True
        with self.assertRaises(CollectionError):
            self.collect(reader)

    def test_revision_change_without_generation_change_rejects_mixed_pods(self):
        reader = FakeReader()
        original = reader.get
        def updating(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if "/deployments/" in path and reader.deployment_reads > 1:
                result["metadata"]["annotations"]["deployment.kubernetes.io/revision"] = "3"
            return result
        with patch.object(reader, "get", side_effect=updating), self.assertRaises(CollectionError):
            self.collect(reader)

    def test_log_collection_requires_opt_in(self):
        reader = FakeReader()
        self.assertEqual(self.collect(reader)["logs"], [])
        self.assertFalse(any(path.endswith("/log") for path, _, _ in reader.calls))

    def test_missing_log_is_explicit_warning_with_status_evidence_preserved(self):
        reader = FakeReader()
        reader.log_error = True
        observation = self.collect(reader, collect_logs=True)
        self.assertEqual(len(observation["pods"]), 1)
        self.assertEqual(len(observation["collection_warnings"]), 1)
        self.assertEqual(observation["logs"], [])

    def test_container_environment_is_not_copied_into_evidence(self):
        reader = FakeReader()
        reader.pods[0]["spec"] = {"containers": [{"env": [{"name": "SECRET", "value": "private-value"}]}]}
        self.assertNotIn("private-value", json.dumps(self.collect(reader)))

    def test_oversized_owned_pod_set_is_not_silently_truncated(self):
        with self.assertRaises(CollectionError):
            self.collect(FakeReader(), max_pods=0)

    def test_expression_selectors_are_supported(self):
        selector = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["demo"]},
                                          {"key": "disabled", "operator": "DoesNotExist"}]}
        self.assertEqual(selector_text(selector), "app in (demo),!disabled")


class ReaderTests(unittest.TestCase):
    def test_kubectl_requires_explicit_context_and_uses_bounded_read_only_command(self):
        calls = []
        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return type("Result", (), {"returncode": 0, "stdout": '{"items":[]}', "stderr": ""})()
        with self.assertRaises(ValueError):
            KubectlReader("")
        self.assertEqual(KubectlReader("lab", runner=runner).get("/api/v1/namespaces/demo/pods"), {"items": []})
        self.assertEqual(calls[0][0][1:4], ["--context", "lab", "--request-timeout=5s"])
        self.assertEqual(calls[0][1]["timeout"], 8)

    def test_projected_token_is_reloaded_per_request(self):
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder) / "token"
            seen = []
            def opener(request, **kwargs):
                seen.append(request.get_header("Authorization"))
                return io.BytesIO(b'{"items":[]}')
            reader = InClusterReader(environ={"KUBERNETES_SERVICE_HOST": "10.0.0.1"}, credentials=folder,
                                     opener=opener, ssl_context=object())
            for value in ("first-token", "rotated-token"):
                token.write_text(value)
                reader.get("/api/v1/namespaces/demo/pods")
        self.assertEqual(seen, ["Bearer first-token", "Bearer rotated-token"])

    def test_api_redirects_are_rejected(self):
        with self.assertRaises(CollectionError):
            NoRedirect().redirect_request(None, None, None, None, None, None)


if __name__ == "__main__":
    unittest.main()
