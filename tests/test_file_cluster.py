"""The file cluster runs the production kubectl adapter with the in-process fakes' semantics."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.distributed import fixtures
from services.executor.core import ACTION_ANNOTATION, ConditionalWriteConflict, encode_evidence
from services.executor.kubernetes_adapter import KubectlDeploymentAPI
from services.executor.reconciliation import FENCE_ANNOTATION, fence_marker
from services.executor.store import fingerprint
from tests.support.executor_fake import FakeDeploymentAPI
from tests.support.file_cluster import FileCluster, patch_action


class FileClusterTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.inputs = fixtures.build()
        self.path = self.directory / "cluster.db"
        self.cluster = self.open(create=True)
        self.target = self.inputs["target"]
        self.template = deepcopy(self.inputs["baseline"]["pod_template"])

    def open(self, *, create=False):
        cluster = (FileCluster.create(self.path, self.inputs["snapshot"]) if create else FileCluster(self.path))
        self.stack.callback(cluster.close)
        return cluster

    def api(self, cluster, host):
        return KubectlDeploymentAPI(context="file-cluster", cluster_id=fixtures.CLUSTER_ID, kubeconfig=str(self.path),
                                    allowed_targets=[self.target], runner=cluster.kubectl(host))

    def evidence(self, action_id="ACT-1", attempt="a"):
        return {"action_id": action_id, "proposal_sha256": "f" * 64, "target_uid": self.target["uid"],
                "template_sha256": fingerprint(self.template), "attempt_id": attempt * 32}

    def restore(self, api, version, **evidence):
        return api.conditional_restore(target=deepcopy(self.target), version=version,
                                       template=deepcopy(self.template), evidence=self.evidence(**evidence))

    def test_the_production_adapter_commits_one_atomic_write_under_a_new_version(self):
        applied = self.restore(self.api(self.cluster, "executor-a"), "102")
        metadata = applied["deployment"]["metadata"]
        self.assertEqual(metadata["resourceVersion"], "103")
        self.assertEqual(metadata["annotations"][ACTION_ANNOTATION], encode_evidence(self.evidence()))
        self.assertEqual(fingerprint(applied["deployment"]["spec"]["template"]), fingerprint(self.template))
        (write,) = self.cluster.committed_writes()
        self.assertEqual((write["host"], write["action_id"], write["from_version"], write["resource_version"]),
                         ("executor-a", "ACT-1", "102", "103"))
        self.assertEqual(write["template_sha256"], fingerprint(self.template))

    def test_a_second_process_sees_the_write_and_its_stale_patch_never_commits(self):
        other = self.open()
        self.restore(self.api(self.cluster, "executor-a"), "102")
        self.assertEqual(other.snapshot(self.target)["deployment"]["metadata"]["resourceVersion"], "103")
        with self.assertRaises(ConditionalWriteConflict):
            self.restore(self.api(other, "executor-b"), "102", attempt="b")
        self.assertEqual(len(self.cluster.committed_writes()), 1)

    def test_a_patch_whose_test_fails_at_the_server_commits_nothing_and_is_recorded(self):
        api = self.api(self.cluster, "executor-a")
        original = self.cluster.run

        def race(command, *, host):
            if "patch" in command:  # Another writer lands between the pre-read and the patch.
                self.cluster.external_write(lambda d: d["metadata"].setdefault("annotations", {}).update(x="1"))
            return original(command, host=host)
        with patch.object(self.cluster, "run", side_effect=race), self.assertRaises(Exception) as caught:
            self.restore(api, "102")
        self.assertEqual(type(caught.exception).__name__, "WriteOutcomeUnknown")
        (request,) = self.cluster.requests("write")
        self.assertEqual(request["state"], "rejected")
        self.assertEqual(self.cluster.committed_writes(), [])

    def test_a_fence_changes_metadata_only(self):
        before = self.cluster.snapshot(self.target)["deployment"]
        fenced = self.api(self.cluster, "executor-a").fence(target=deepcopy(self.target), version="102",
                                                            marker=fence_marker("ACT-1", "c" * 32))
        after = fenced["deployment"]
        self.assertEqual(after["spec"], before["spec"])
        self.assertIn(FENCE_ANNOTATION, after["metadata"]["annotations"])
        self.assertEqual([row["kind"] for row in self.cluster.requests()], ["fence"])
        self.assertEqual(self.cluster.committed_writes(), [])

    def test_versions_never_repeat_and_match_the_in_process_fake_outcomes(self):
        fake = FakeDeploymentAPI(self.inputs["snapshot"])
        file_api = self.api(self.cluster, "executor-a")
        outcomes = {}
        for name, api in (("fake", fake), ("file", file_api)):
            steps = [api.conditional_restore(target=deepcopy(self.target), version="102",
                                             template=deepcopy(self.template), evidence=self.evidence())]
            with self.assertRaises(ConditionalWriteConflict):
                api.conditional_restore(target=deepcopy(self.target), version="102",
                                        template=deepcopy(self.template), evidence=self.evidence(attempt="b"))
            current = api.read(self.target)["deployment"]["metadata"]["resourceVersion"]
            steps.append(api.fence(target=deepcopy(self.target), version=current,
                                   marker=fence_marker("ACT-1", "d" * 32)))
            versions = ["102"] + [step["deployment"]["metadata"]["resourceVersion"] for step in steps]
            self.assertEqual(len(set(versions)), len(versions))
            outcomes[name] = [(fingerprint(step["deployment"]["spec"]["template"]),
                               sorted(step["deployment"]["metadata"]["annotations"])) for step in steps]
        self.assertEqual(outcomes["fake"], outcomes["file"])

    def test_create_refuses_to_overwrite_and_an_absent_file_is_not_a_cluster(self):
        with self.assertRaises(ValueError):
            FileCluster.create(self.path, self.inputs["snapshot"])
        empty = self.open_empty()
        with self.assertRaises(ValueError):
            empty.snapshot()

    def open_empty(self):
        cluster = FileCluster(self.directory / "empty.db")
        self.stack.callback(cluster.close)
        return cluster

    def test_health_follows_the_template_and_lab_readers_take_spaced_samples(self):
        baseline_sha = self.inputs["baseline"]["template_sha256"]
        self.assertEqual(self.cluster.health_sample(self.target, baseline_sha, ["mock-db"])["http_status"], 503)
        self.restore(self.api(self.cluster, "executor-a"), "102")
        healthy = self.cluster.health_sample(self.target, baseline_sha, ["mock-db"])
        self.assertEqual((healthy["http_status"], healthy["ready_replicas"]), (200, 1))
        paths = {}
        for name in ("baseline", "graph"):
            paths[name] = self.directory / (name + ".json")
            paths[name].write_text(__import__("json").dumps(self.inputs[name]), encoding="utf-8")
        from runtime.remote_adapters import utc_now
        samples, dependencies = self.cluster.verifier_readers(
            {"baseline": paths["baseline"], "dependency_graph": paths["graph"], "samples_per_pass": 3,
             "sample_spacing_seconds": 0.01}, clock=utc_now)
        with patch("runtime.fault_points.checkpoint") as checkpoint:
            readings = samples({"target": self.target})
        checkpoint.assert_called_once_with("verifier.mid_collection")
        self.assertEqual([reading["http_status"] for reading in readings], [200, 200, 200])
        self.assertEqual(sorted({reading["at"] for reading in readings}), [reading["at"] for reading in readings])
        proposal = __import__("json").loads(
            (Path(fixtures.SAMPLES) / "messages" / "proposal.json").read_text(encoding="utf-8"))
        proposal["target"] = deepcopy(self.target)
        self.assertEqual(dependencies(proposal)["kind"], "dependency_result")
        with self.assertRaises(ValueError):
            self.cluster.verifier_readers({"baseline": paths["baseline"], "dependency_graph": paths["graph"],
                                           "samples_per_pass": 0}, clock=utc_now)

    def test_patch_actions_are_classified_from_the_annotation(self):
        evidence = encode_evidence(self.evidence("ACT-9"))
        self.assertEqual(patch_action([{"op": "add", "path": "/metadata/annotations",
                                        "value": {ACTION_ANNOTATION: evidence}}]), ("write", "ACT-9"))
        self.assertEqual(patch_action([{"op": "add", "path": "/metadata/annotations/sentinel.io~1fence",
                                        "value": '{"action_id":"ACT-8","nonce":"n"}'}]), ("fence", "ACT-8"))
        self.assertEqual(patch_action([{"op": "add", "path": "/metadata/annotations/sentinel.io~1fence",
                                        "value": "not json"}]), ("fence", "unknown"))
        self.assertEqual(patch_action("nonsense"), ("other", "unknown"))


if __name__ == "__main__":
    unittest.main()
