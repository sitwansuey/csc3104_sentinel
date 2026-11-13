"""Live adapter command/patch checks with an injected process runner.

These tests make no cluster calls. The separate live experiment exercises the
same transport with a real restricted Kubernetes identity.
"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from experiments.validation.demo_guard import load_fixtures
from services.executor.core import (
    ACTION_ANNOTATION, ConditionalWriteConflict, ReadUnavailable, TargetAbsent, encode_evidence,
)
from services.executor.store import canonical_json, fingerprint
from services.executor.kubernetes_adapter import KubectlDeploymentAPI, LiveAdapterError, WriteOutcomeUnknown
from services.executor.reconciliation import FENCE_ANNOTATION, fence_marker


NOW = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)


class Runner:
    """Inspect private patch files while they exist and supply API responses."""

    def __init__(self, deployment):
        self.deployment = deepcopy(deployment)
        self.commands = []
        self.patches = []
        self.patch_paths = []
        self.read_error = None
        self.patch_error = None
        self.response_change = None

    def __call__(self, command, **kwargs):
        self.commands.append((deepcopy(command), kwargs))
        if "get" in command:
            if self.read_error is not None:
                if isinstance(self.read_error, BaseException):
                    raise self.read_error
                return self.read_error
            return subprocess.CompletedProcess(command, 0, json.dumps(self.deployment).encode(), b"")
        if "patch" not in command:
            raise AssertionError("Unexpected mutating command")
        patch_path = Path(command[command.index("--patch-file") + 1])
        self.patch_paths.append(patch_path)
        self.patches.append(json.loads(patch_path.read_text(encoding="utf-8")))
        if self.patch_error is not None:
            if isinstance(self.patch_error, BaseException):
                raise self.patch_error
            return self.patch_error
        candidate = deepcopy(self.deployment)
        for operation in self.patches[-1]:
            if operation["path"] == "/spec/template":
                candidate["spec"]["template"] = deepcopy(operation["value"])
            if operation["path"] == "/spec/replicas":  # A SCALE (P5.07).
                candidate["spec"]["replicas"] = operation["value"]
        for annotation_operation in self.patches[-1]:
            if not annotation_operation["path"].startswith("/metadata/annotations"):
                continue
            if annotation_operation["path"] == "/metadata/annotations":
                candidate["metadata"]["annotations"] = annotation_operation["value"]
            else:
                key = annotation_operation["path"].rsplit("/", 1)[1].replace("~1", "/").replace("~0", "~")
                candidate["metadata"].setdefault("annotations", {})[key] = annotation_operation["value"]
        candidate["metadata"]["resourceVersion"] = "server-write-1"
        if self.response_change:
            self.response_change(candidate)
        self.deployment = candidate
        return subprocess.CompletedProcess(command, 0, json.dumps(candidate).encode(), b"")


class KubernetesExecutorAdapterTests(unittest.TestCase):
    def setUp(self):
        fixtures = load_fixtures()
        self.target = fixtures["proposal"]["target"]
        self.deployment = fixtures["live_state"]["deployment"]
        self.template = fixtures["baseline"]["pod_template"]
        self.version = self.deployment["metadata"]["resourceVersion"]
        self.evidence = {"action_id": "ACT-LIVE-001", "proposal_sha256": "a" * 64,
                         "template_sha256": fingerprint(self.template), "target_uid": self.target["uid"],
                         "attempt_id": "b" * 32}
        self.runner = Runner(self.deployment)
        self.adapter = self.make_adapter()

    def make_adapter(self, **changes):
        values = dict(context="kind-team", cluster_id=self.target["cluster_id"],
                      kubeconfig="restricted credentials.json", allowed_targets=[self.target],
                      runner=self.runner, clock=lambda: NOW)
        values.update(changes)
        return KubectlDeploymentAPI(**values)

    def restore(self, adapter=None, **changes):
        values = dict(target=self.target, version=self.version, template=self.template, evidence=self.evidence)
        values.update(changes)
        return (adapter or self.adapter).conditional_restore(**values)

    def test_constructor_requires_explicit_credentials_cluster_and_targets_without_api_calls(self):
        for changes in ({"context": ""}, {"cluster_id": ""}, {"kubeconfig": None},
                        {"kubeconfig": ""}, {"allowed_targets": []}, {"kubectl": ""}):
            with self.subTest(changes=changes), self.assertRaises(LiveAdapterError):
                self.make_adapter(**changes)
        self.assertEqual(self.runner.commands, [])

    def test_read_is_explicit_bounded_and_does_not_change_environment(self):
        environment = dict(os.environ)
        snapshot = self.adapter.read(self.target)
        self.assertEqual(snapshot["deployment"], self.deployment)
        self.assertEqual(snapshot["cluster_id"], self.target["cluster_id"])
        self.assertEqual(snapshot["captured_at"], "2026-10-25T00:00:00Z")
        command, options = self.runner.commands[0]
        self.assertEqual(command, ["kubectl", "--context=kind-team", "--kubeconfig=restricted credentials.json",
                                   "--namespace=sentinel-demo", "--request-timeout=10s",
                                   "get", "deployment", "demo-api", "--ignore-not-found", "--output=json"])
        self.assertEqual(options, {"capture_output": True, "check": False, "timeout": 15})
        self.assertEqual(dict(os.environ), environment)
        self.assertEqual(self.adapter.stats["read_attempts"], 1)

    def test_disallowed_target_and_option_like_names_are_refused_before_any_command(self):
        cases = (("namespace", "--namespace=other"), ("name", "--all"), ("name", "a..b"),
                 ("kind", "Secret"), ("cluster_id", "other"), ("uid", "unapproved-uid"),
                 ("namespace", "other-namespace"), ("name", "another-deployment"))
        for key, value in cases:
            target = dict(self.target, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(LiveAdapterError):
                self.adapter.read(target)
        self.assertEqual(self.runner.commands, [])

    def test_allowlist_is_copied_from_the_callers_configuration(self):
        configured = deepcopy(self.target)
        adapter = self.make_adapter(allowed_targets=[configured])
        configured["name"] = "another-deployment"
        with self.assertRaises(LiveAdapterError):
            adapter.read(configured)
        self.assertEqual(self.runner.commands, [])

    def test_read_preserves_recreated_uid_for_executor_conflict_and_reconciliation(self):
        self.runner.deployment["metadata"]["uid"] = "new-deployment-uid"
        self.assertEqual(self.adapter.read(self.target)["deployment"]["metadata"]["uid"], "new-deployment-uid")

    def test_restore_patches_exact_template_and_evidence_with_both_atomic_tests(self):
        applied = self.restore()
        self.assertEqual(self.runner.patches[0][:3], [
            {"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.version},
            {"op": "replace", "path": "/spec/template", "value": self.template},
        ])
        self.assertEqual(applied["deployment"]["spec"]["template"], self.template)
        self.assertEqual(applied["deployment"]["metadata"]["annotations"][ACTION_ANNOTATION],
                         encode_evidence(self.evidence))
        command = self.runner.commands[-1][0]
        self.assertIn("--type=json", command)
        self.assertIn("--output=json", command)
        self.assertFalse(any("dry-run" in argument or argument in {"undo", "delete", "replace", "scale"}
                             for argument in command))
        self.assertEqual(self.adapter.stats, {"read_attempts": 1, "write_attempts": 1, "successful_writes": 1,
                                              "fence_attempts": 0, "successful_fences": 0})
        self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    def test_annotation_update_preserves_unrelated_annotations_and_escapes_the_key(self):
        self.runner.deployment["metadata"]["annotations"] = {"example.org/owner": "team", ACTION_ANNOTATION: "older"}
        applied = self.restore()
        operation = self.runner.patches[0][-1]
        self.assertEqual(operation["path"], "/metadata/annotations/sentinel.io~1execution")
        self.assertEqual(applied["deployment"]["metadata"]["annotations"]["example.org/owner"], "team")

    def test_absent_annotations_are_added_in_the_same_patch(self):
        self.runner.deployment["metadata"].pop("annotations", None)
        self.restore()
        self.assertEqual(self.runner.patches[0][-1], {
            "op": "add", "path": "/metadata/annotations", "value": {ACTION_ANNOTATION: encode_evidence(self.evidence)},
        })

    def test_stale_version_or_uid_is_definitely_a_zero_write_local_conflict(self):
        for field in ("uid", "resourceVersion"):
            with self.subTest(field=field):
                self.runner = Runner(self.deployment)
                self.runner.deployment["metadata"][field] = "changed"
                adapter = self.make_adapter()
                with self.assertRaises(ConditionalWriteConflict):
                    self.restore(adapter)
                self.assertEqual(adapter.stats["write_attempts"], 0)
                self.assertEqual(self.runner.patches, [])

    def test_evidence_must_bind_uid_template_hash_and_attempt_nonce_before_any_api_call(self):
        for changes in ({"target_uid": "wrong"}, {"template_sha256": "a" * 64},
                        {"proposal_sha256": "invalid"}, {"attempt_id": "not-a-nonce"},
                        {"action_id": ""}, {"command": "unwanted"}):
            with self.subTest(changes=changes), self.assertRaises(LiveAdapterError):
                self.restore(evidence=dict(self.evidence, **changes))
        self.assertEqual(self.runner.commands, [])

    def test_patch_nonzero_response_is_unknown_and_never_retried_or_echoed(self):
        self.runner.patch_error = subprocess.CompletedProcess([], 1, b"", b"PRIVATE-SERVER-DIAGNOSTICS")
        with self.assertRaises(WriteOutcomeUnknown) as failure:
            self.restore()
        self.assertNotIn("PRIVATE", str(failure.exception))
        self.assertEqual(self.adapter.stats["write_attempts"], 1)
        self.assertEqual(len(self.runner.patches), 1)
        self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    def test_patch_timeout_and_process_error_are_unknown_without_mutating_retry(self):
        for error in (subprocess.TimeoutExpired(["kubectl"], 15, output=b"PRIVATE", stderr=b"PRIVATE"),
                      FileNotFoundError("PRIVATE PATH")):
            with self.subTest(error=type(error).__name__):
                self.runner = Runner(self.deployment)
                self.runner.patch_error = error
                adapter = self.make_adapter()
                with self.assertRaises(WriteOutcomeUnknown) as failure:
                    self.restore(adapter)
                self.assertNotIn("PRIVATE", str(failure.exception))
                self.assertEqual(adapter.stats["write_attempts"], 1)
                self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    def test_read_transport_failures_are_side_effect_free_and_have_safe_errors(self):
        errors = (subprocess.CompletedProcess([], 1, b"PRIVATE", b"PRIVATE"),
                  subprocess.TimeoutExpired(["kubectl"], 15, output=b"PRIVATE"),
                  OSError("PRIVATE PATH"))
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.runner.read_error = error
                with self.assertRaises(ReadUnavailable) as failure:
                    self.adapter.read(self.target)
                self.assertNotIn("PRIVATE", str(failure.exception))
        self.assertEqual(self.adapter.stats["write_attempts"], 0)

    def test_only_empty_success_output_means_the_target_is_absent(self):
        for empty in (b"", b"\n", b"  \r\n"):
            with self.subTest(empty=empty):
                self.runner.read_error = subprocess.CompletedProcess([], 0, empty, b"")
                with self.assertRaises(TargetAbsent):
                    self.adapter.read(self.target)
        # A failing command is never read as absence, even with empty output.
        self.runner.read_error = subprocess.CompletedProcess([], 1, b"", b"")
        with self.assertRaises(ReadUnavailable):
            self.adapter.read(self.target)
        self.runner.read_error = subprocess.CompletedProcess([], 0, b"garbage", b"")
        with self.assertRaises(LiveAdapterError):
            self.adapter.read(self.target)
        self.assertFalse(issubclass(TargetAbsent, ReadUnavailable))

    def test_absent_target_before_submission_is_a_zero_write_conflict(self):
        self.runner.read_error = subprocess.CompletedProcess([], 0, b"", b"")
        with self.assertRaises(ConditionalWriteConflict):
            self.restore()
        self.assertEqual(self.adapter.stats["write_attempts"], 0)
        self.assertEqual(self.runner.patches, [])

    def test_malformed_or_duplicate_json_and_wrong_identity_cannot_be_live_state(self):
        for raw in (b"not JSON", b'{"metadata":{},"metadata":{}}', b'{"value":NaN}', b"\xff"):
            with self.subTest(raw=raw):
                self.runner.read_error = subprocess.CompletedProcess([], 0, raw, b"")
                with self.assertRaises(LiveAdapterError):
                    self.adapter.read(self.target)
        self.runner.read_error = None
        self.runner.deployment["metadata"]["namespace"] = "wrong"
        with self.assertRaises(LiveAdapterError):
            self.adapter.read(self.target)

    def test_invalid_success_response_remains_unknown_without_a_second_mutation(self):
        def wrong_uid(candidate):
            candidate["metadata"]["uid"] = "wrong"

        def unchanged_version(candidate):
            candidate["metadata"]["resourceVersion"] = self.version

        def wrong_template(candidate):
            candidate["spec"]["template"]["spec"]["containers"][0]["image"] = "changed:latest"

        def wrong_evidence(candidate):
            candidate["metadata"]["annotations"][ACTION_ANNOTATION] = "another-action"

        for change in (wrong_uid, unchanged_version, wrong_template, wrong_evidence):
            with self.subTest(change=change.__name__):
                self.runner = Runner(self.deployment)
                self.runner.response_change = change
                adapter = self.make_adapter()
                with self.assertRaises(WriteOutcomeUnknown):
                    self.restore(adapter)
                self.assertEqual(adapter.stats["write_attempts"], 1)
                self.assertEqual(adapter.stats["successful_writes"], 0)
                self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    # Fence -----------------------------------------------------------------

    def fence(self, adapter=None, **changes):
        values = dict(target=self.target, version=self.version, marker=fence_marker("ACT-LIVE-001", "c" * 32))
        values.update(changes)
        return (adapter or self.adapter).fence(**values)

    def test_fence_patch_is_two_tests_and_one_annotation_and_never_touches_spec(self):
        self.runner.deployment["metadata"]["annotations"] = {"example.org/owner": "team", ACTION_ANNOTATION: "older"}
        fenced = self.fence()
        marker = canonical_json(fence_marker("ACT-LIVE-001", "c" * 32))
        self.assertEqual(self.runner.patches, [[
            {"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.version},
            {"op": "add", "path": "/metadata/annotations/sentinel.io~1fence", "value": marker},
        ]])
        self.assertFalse(any(operation["path"].startswith("/spec") for operation in self.runner.patches[0]))
        annotations = fenced["deployment"]["metadata"]["annotations"]
        self.assertEqual(annotations, {"example.org/owner": "team", ACTION_ANNOTATION: "older",
                                       FENCE_ANNOTATION: marker})
        self.assertEqual(fenced["deployment"]["spec"]["template"], self.deployment["spec"]["template"])
        # No spec change, so no rollout (a real Deployment still bumps generation for the annotation).
        self.assertEqual(fenced["deployment"]["spec"], self.deployment["spec"])
        command = self.runner.commands[-1][0]
        self.assertEqual(command[5:8], ["patch", "deployment", "demo-api"])
        self.assertIn("--type=json", command)
        self.assertFalse(any("dry-run" in argument for argument in command))
        self.assertEqual(self.adapter.stats, {"read_attempts": 1, "write_attempts": 0, "successful_writes": 0,
                                              "fence_attempts": 1, "successful_fences": 1})
        self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    def test_fence_on_a_deployment_without_annotations_adds_the_map(self):
        self.runner.deployment["metadata"].pop("annotations", None)
        self.fence(marker=fence_marker("ACT-LIVE-001", "c" * 32, epoch=3))
        self.assertEqual(self.runner.patches[0][-1], {
            "op": "add", "path": "/metadata/annotations",
            "value": {FENCE_ANNOTATION: '{"action_id":"ACT-LIVE-001","epoch":3,"nonce":"' + "c" * 32 + '"}',
                      "sentinel.io/fence-epoch": "3"}})

    def test_a_seal_patch_is_the_uid_and_version_tests_plus_its_marker_and_epoch_only(self):
        # P3.04: a Coordinator seal is fence() with an epoch; it adds sentinel.io/fence-epoch and touches no spec.
        self.runner.deployment["metadata"]["annotations"] = {"example.org/owner": "team"}
        marker = fence_marker("ACT-LIVE-001", "d" * 32, epoch=7)
        fenced = self.fence(marker=marker)
        self.assertEqual(self.runner.patches, [[
            {"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.version},
            {"op": "add", "path": "/metadata/annotations/sentinel.io~1fence", "value": canonical_json(marker)},
            {"op": "add", "path": "/metadata/annotations/sentinel.io~1fence-epoch", "value": "7"},
        ]])
        self.assertEqual(fenced["deployment"]["metadata"]["annotations"]["sentinel.io/fence-epoch"], "7")
        self.assertEqual(fenced["deployment"]["spec"], self.deployment["spec"])

    def test_a_seal_outside_the_allowlist_is_refused_before_any_command(self):
        outside = dict(self.target, name="other-api")
        with self.assertRaises(LiveAdapterError):
            self.adapter.fence(target=outside, version=self.version, marker=fence_marker("ACT-LIVE-001", "d" * 32, 2))
        self.assertEqual(self.runner.commands, [])

    def test_fence_uses_only_the_get_and_patch_verbs_the_executor_role_grants(self):
        self.fence()
        verbs = {command[5] for command, _ in self.runner.commands}
        self.assertEqual(verbs, {"get", "patch"})
        role = (Path(__file__).resolve().parents[1] / "deploy/sentinel/executor-rbac.yaml").read_text(encoding="utf-8")
        rules = role.split("kind: Role\n", 1)[1].split("---", 1)[0]
        self.assertEqual(rules.count("- apiGroups:"), 1)
        self.assertIn('resources: ["deployments"]', rules)
        self.assertIn('resourceNames: ["demo-api"]', rules)
        self.assertIn('verbs: ["get", "patch"]', rules)

    def test_invalid_fence_marker_or_version_is_refused_before_any_command(self):
        nonce = "c" * 32
        for changes in ({"marker": {"action_id": "A", "nonce": "short"}}, {"marker": {"action_id": "", "nonce": nonce}},
                        {"marker": {"action_id": "A", "nonce": nonce, "epoch": True}},
                        {"marker": {"action_id": "A", "nonce": nonce, "epoch": -1}},
                        {"marker": {"action_id": "A", "nonce": nonce, "command": "x"}},
                        {"marker": {"nonce": nonce}}, {"marker": "not a marker"}, {"version": ""},
                        {"target": dict(self.target, name="another-deployment")}):
            with self.subTest(changes=changes), self.assertRaises(LiveAdapterError):
                self.fence(**changes)
        self.assertEqual(self.runner.commands, [])

    def test_fence_precondition_mismatch_or_absent_target_is_a_zero_write_conflict(self):
        for field in ("uid", "resourceVersion", None):
            with self.subTest(field=field):
                self.runner = Runner(self.deployment)
                if field is None:
                    self.runner.read_error = subprocess.CompletedProcess([], 0, b"", b"")
                else:
                    self.runner.deployment["metadata"][field] = "changed"
                adapter = self.make_adapter()
                with self.assertRaises(ConditionalWriteConflict):
                    self.fence(adapter)
                self.assertEqual((adapter.stats["fence_attempts"], self.runner.patches), (0, []))

    def test_fence_error_or_timeout_is_unknown_never_retried_or_echoed(self):
        for error in (subprocess.CompletedProcess([], 1, b"", b"PRIVATE-SERVER-DIAGNOSTICS"),
                      subprocess.TimeoutExpired(["kubectl"], 15, output=b"PRIVATE"), OSError("PRIVATE PATH")):
            with self.subTest(error=type(error).__name__):
                self.runner = Runner(self.deployment)
                self.runner.patch_error = error
                adapter = self.make_adapter()
                with self.assertRaises(WriteOutcomeUnknown) as failure:
                    self.fence(adapter)
                self.assertNotIn("PRIVATE", str(failure.exception))
                self.assertEqual((adapter.stats["fence_attempts"], adapter.stats["successful_fences"]), (1, 0))
                self.assertEqual(len(self.runner.patches), 1)
                self.assertTrue(all(not path.exists() for path in self.runner.patch_paths))

    def test_fence_success_response_must_show_the_marker_and_a_new_version(self):
        def no_marker(candidate):
            candidate["metadata"]["annotations"].pop(FENCE_ANNOTATION)

        def unchanged_version(candidate):
            candidate["metadata"]["resourceVersion"] = self.version

        for change in (no_marker, unchanged_version):
            with self.subTest(change=change.__name__):
                self.runner = Runner(self.deployment)
                self.runner.response_change = change
                adapter = self.make_adapter()
                with self.assertRaises(WriteOutcomeUnknown):
                    self.fence(adapter)
                self.assertEqual(adapter.stats["successful_fences"], 0)

    def test_private_file_failure_prevents_patch_submission_and_cleans_empty_file(self):
        created = []

        def refuse(path):
            created.append(path)
            raise OSError("Cannot set private permissions")

        with patch("services.executor.kubernetes_adapter._private_file", side_effect=refuse):
            with self.assertRaises(OSError):
                self.restore()
        self.assertEqual(self.adapter.stats["write_attempts"], 0)
        self.assertTrue(created)
        self.assertTrue(all(not path.exists() for path in created))


if __name__ == "__main__":
    unittest.main()
