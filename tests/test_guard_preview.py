"""Kubernetes admission preview safety without contacting a real cluster."""

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.guard.core import proposal_fingerprint, template_sha256
from services.guard.adapters import AdapterError, KUBECTL_TIMEOUT_SECONDS
from services.guard.preview import server_dry_run
from shared.actions import restart_parameters, restart_template, restart_token


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class GuardPreviewTests(unittest.TestCase):
    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.response = copy.deepcopy(self.live["deployment"])
        self.response["spec"]["template"] = copy.deepcopy(self.baseline["pod_template"])

    def preview(self, **kwargs):
        return server_dry_run(self.proposal, self.live, self.baseline,
                              context="team-context", cluster_id="sentinel-lab", **kwargs)

    def completed(self, response=None):
        response = self.response if response is None else response
        return subprocess.CompletedProcess([], 0, json.dumps(response).encode("utf-8"), b"")

    def test_exact_conditional_patch_is_private_closed_and_always_server_dry_run(self):
        observed = {}

        def run(command, **kwargs):
            observed["path"] = Path(command[command.index("--patch-file") + 1])
            # Reopen during subprocess execution: Windows rejects this if the
            # creator still has a conflicting temporary-file handle open.
            with observed["path"].open("rb") as stream:
                observed["patch"] = json.load(stream)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(observed["path"].stat().st_mode), 0o600)
            self.assertEqual(command, [
                "kubectl", "--context=team-context", "--namespace=sentinel-demo",
                "--request-timeout=10s", "--kubeconfig=C:/private config/team.yaml",
                "patch", "deployment", "demo-api", "--type=json", "--patch-file",
                str(observed["path"]), "--dry-run=server", "--output=json",
            ])
            self.assertEqual(kwargs, {"capture_output": True, "check": False,
                                      "timeout": KUBECTL_TIMEOUT_SECONDS})
            return self.completed()

        before = datetime.now(timezone.utc)
        with patch("services.guard.preview.subprocess.run", side_effect=run) as mocked:
            result = self.preview(kubeconfig="C:/private config/team.yaml")
        mocked.assert_called_once()
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["details_code"], "SERVER_DRY_RUN_PASSED")
        self.assertEqual(result["proposal_sha256"], proposal_fingerprint(self.proposal))
        self.assertEqual(result["target"], self.proposal["target"])
        self.assertEqual(result["resource_version"], "102")
        self.assertEqual(result["template_sha256"], self.baseline["template_sha256"])
        self.assertEqual(result["admitted_template_sha256"], self.baseline["template_sha256"])
        self.assertEqual(observed["patch"], [
            {"op": "test", "path": "/metadata/uid", "value": self.proposal["target"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "102"},
            {"op": "replace", "path": "/spec/template", "value": self.baseline["pod_template"]},
        ])
        self.assertFalse(observed["path"].exists())
        checked = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, checked)
        self.assertLessEqual(checked, datetime.now(timezone.utc))

    def test_stale_uid_version_namespace_kind_and_cluster_never_make_request(self):
        mutations = [
            lambda: self.live.update(cluster_id="other"),
            lambda: self.proposal["target"].update(cluster_id="other"),
            lambda: self.live["deployment"]["metadata"].update(uid="replacement"),
            lambda: self.live["deployment"]["metadata"].update(resourceVersion="103"),
            lambda: self.live["deployment"]["metadata"].update(namespace="other"),
            lambda: self.live["deployment"]["metadata"].update(name="other"),
            lambda: self.live["deployment"].update(kind="ReplicaSet"),
            lambda: self.live["deployment"].update(apiVersion="v1"),
            lambda: self.live.update(deployment=None),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp()
                mutate()
                with patch("services.guard.preview.subprocess.run") as run:
                    result = self.preview()
                run.assert_not_called()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], "LIVE_PRECONDITION_FAILED")

    def test_tampered_or_wrong_baseline_never_makes_request(self):
        mutations = [
            lambda: self.baseline["pod_template"]["spec"]["containers"][0].update(image="tampered"),
            lambda: self.baseline.update(template_sha256="0" * 64),
            lambda: self.baseline.update(baseline_id="wrong"),
            lambda: self.baseline["target"].update(uid="wrong"),
            lambda: self.baseline.update(pod_template=None),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                self.setUp()
                mutate()
                with patch("services.guard.preview.subprocess.run") as run:
                    result = self.preview()
                run.assert_not_called()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], "BASELINE_PRECONDITION_FAILED")

    def test_malformed_proposal_or_unsafe_target_never_calls_kubectl(self):
        for field in ("namespace", "name"):
            for value in ("--raw=/secret", "name with spaces", "bad..name"):
                with self.subTest(field=field, value=value):
                    self.setUp()
                    self.proposal["target"][field] = value
                    with patch("services.guard.preview.subprocess.run") as run:
                        with self.assertRaises(AdapterError):
                            self.preview()
                    run.assert_not_called()
        self.setUp()
        self.proposal["unexpected-secret"] = "do-not-echo"
        with patch("services.guard.preview.subprocess.run") as run:
            with self.assertRaises(AdapterError) as caught:
                self.preview()
        run.assert_not_called()
        self.assertNotIn("do-not-echo", str(caught.exception))
        self.assertEqual(caught.exception.code, "INVALID_PREVIEW_INPUT")

    def test_missing_explicit_context_cluster_or_kubeconfig_fails_before_request(self):
        for args in ({"context": "", "cluster_id": "sentinel-lab"},
                     {"context": "team-context", "cluster_id": " "},
                     {"context": "team-context", "cluster_id": "sentinel-lab", "kubeconfig": ""}):
            with self.subTest(args=args):
                with patch("services.guard.preview.subprocess.run") as run:
                    with self.assertRaises(AdapterError):
                        server_dry_run(self.proposal, self.live, self.baseline, **args)
                run.assert_not_called()

    def test_authentication_and_ambiguous_server_rejections_are_unknown_without_stderr(self):
        for stderr in (b"Unauthorized secret-token", b"Forbidden secret-token", b"invalid: secret-token"):
            with self.subTest(stderr=stderr):
                failure = subprocess.CompletedProcess([], 1, b"", stderr)
                with patch("services.guard.preview.subprocess.run", return_value=failure) as run:
                    result = self.preview()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], "SERVER_DRY_RUN_FAILED")
                self.assertNotIn("secret-token", json.dumps(result))
                self.assertFalse(Path(run.call_args.args[0][-3]).exists())
                self.assertEqual(run.call_count, 1)

    def test_an_admission_policy_denial_is_unknown_not_rejected(self):
        # A ValidatingAdmissionPolicy in Deny mode (deploy/security/approved-images-vap.yaml)
        # makes the dry-run exit non-zero. kubectl's text cannot be trusted to
        # tell a denial from other failures, so the preview stays UNKNOWN.
        stderr = (b"The deployments \"checkout\" is invalid: : ValidatingAdmissionPolicy "
                  b"'sentinel-approved-images' with binding 'sentinel-approved-images' denied request: "
                  b"Every container image must be an approved repository@sha256 digest")
        failure = subprocess.CompletedProcess([], 1, b"", stderr)
        with patch("services.guard.preview.subprocess.run", return_value=failure):
            result = self.preview()
        self.assertEqual((result["status"], result["details_code"]), ("UNKNOWN", "SERVER_DRY_RUN_FAILED"))
        self.assertIsNone(result["admitted_template_sha256"])

    def test_timeout_and_unavailable_kubectl_cleanup_without_fallback(self):
        for failure, detail in (
            (subprocess.TimeoutExpired(["private-command"], 15, stderr=b"secret"), "SERVER_DRY_RUN_TIMEOUT"),
            (FileNotFoundError("private-system-path"), "SERVER_DRY_RUN_UNAVAILABLE"),
        ):
            with self.subTest(failure=failure):
                with patch("services.guard.preview.subprocess.run", side_effect=failure) as run:
                    result = self.preview()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], detail)
                self.assertNotIn("secret", json.dumps(result))
                command = run.call_args.args[0]
                self.assertIn("--dry-run=server", command)
                self.assertFalse(Path(command[command.index("--patch-file") + 1]).exists())
                run.assert_called_once()

    def test_privacy_failure_prevents_subprocess_and_cleans_empty_file(self):
        observed = []

        def fail(path):
            observed.append(path)
            self.assertEqual(path.read_bytes(), b"")
            raise OSError("private-security-details")

        with patch("services.guard.preview._private_file", side_effect=fail):
            with patch("services.guard.preview.subprocess.run") as run:
                result = self.preview()
        run.assert_not_called()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["details_code"], "SERVER_DRY_RUN_UNAVAILABLE")
        self.assertFalse(observed[0].exists())

    def test_strict_bounded_server_json_and_invalid_object_are_unknown(self):
        for payload in (b"not-json", b"\xff", b'{"x":1,"x":2}', b'{"x":NaN}', b"null", b"[]"):
            with self.subTest(payload=payload):
                with patch("services.guard.preview.subprocess.run", return_value=subprocess.CompletedProcess([], 0, payload, b"secret")):
                    result = self.preview()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertNotIn("secret", json.dumps(result))
        with patch("services.guard.adapters.MAX_JSON_BYTES", 10):
            with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
                result = self.preview()
        self.assertEqual(result["details_code"], "INVALID_SERVER_RESPONSE")

    def test_response_identity_or_version_changes_cannot_pass(self):
        for field, value in (("uid", "replacement"), ("resourceVersion", "103"),
                             ("namespace", "other"), ("name", "other")):
            with self.subTest(field=field):
                self.setUp()
                self.response["metadata"][field] = value
                with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
                    result = self.preview()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], "SERVER_RESPONSE_IDENTITY_MISMATCH")

    def test_missing_response_template_cannot_pass(self):
        del self.response["spec"]["template"]
        with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
            result = self.preview()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["details_code"], "INVALID_SERVER_RESPONSE")

    def test_admission_mutation_requires_new_candidate_and_approval(self):
        self.response["spec"]["template"]["metadata"]["annotations"] = {"injected": "true"}
        with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
            result = self.preview()
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["details_code"], "ADMISSION_TEMPLATE_MUTATED")
        self.assertEqual(result["admitted_template_sha256"], template_sha256(self.response["spec"]["template"]))
        self.assertNotEqual(result["admitted_template_sha256"], result["template_sha256"])

    def test_rollback_hash_not_matching_the_baseline_never_makes_request(self):
        self.proposal["parameters"]["template_sha256"] = "b" * 64
        with patch("services.guard.preview.subprocess.run") as run:
            result = self.preview()
        run.assert_not_called()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["details_code"], "BASELINE_PRECONDITION_FAILED")
        self.assertEqual(result["template_sha256"], "b" * 64)

    def test_an_action_without_a_write_is_not_previewed_and_never_makes_request(self):
        # SCALE has its own preview since P5.07 (tests/test_scale_action.py); NO_ACTION writes nothing.
        self.proposal.update(action="NO_ACTION", parameters={})
        with patch("services.guard.preview.subprocess.run") as run:
            result = self.preview()
        run.assert_not_called()
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["details_code"], "UNSUPPORTED_PREVIEW_ACTION")
        self.assertIsNone(result["template_sha256"])


class RestartPreviewTests(GuardPreviewTests):
    """A RESTART previews the reviewed baseline plus its planner-bound token, and nothing else."""

    def setUp(self):
        super().setUp()
        self.proposal.update(action="RESTART",
                             parameters=restart_parameters(self.proposal["incident_id"], self.baseline))
        self.token = self.proposal["parameters"]["restart_token"]
        self.candidate = restart_template(self.baseline["pod_template"], self.token)
        self.response["spec"]["template"] = copy.deepcopy(self.candidate)

    def test_exact_conditional_patch_is_private_closed_and_always_server_dry_run(self):
        observed = {}

        def run(command, **kwargs):
            with Path(command[command.index("--patch-file") + 1]).open("rb") as stream:
                observed["patch"] = json.load(stream)
            observed["command"] = command
            return self.completed()

        with patch("services.guard.preview.subprocess.run", side_effect=run) as mocked:
            result = self.preview()
        mocked.assert_called_once()
        self.assertEqual(observed["command"][-2:], ["--dry-run=server", "--output=json"])
        self.assertEqual(observed["patch"], [
            {"op": "test", "path": "/metadata/uid", "value": self.proposal["target"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "102"},
            {"op": "replace", "path": "/spec/template", "value": self.candidate},
        ])
        self.assertEqual(self.candidate["metadata"]["annotations"]["sentinel.io/restart-token"], self.token)
        self.assertEqual(result["status"], "PASSED")
        self.assertEqual(result["details_code"], "SERVER_DRY_RUN_PASSED")
        self.assertEqual(result["proposal_sha256"], proposal_fingerprint(self.proposal))
        self.assertEqual(result["template_sha256"], self.proposal["parameters"]["template_sha256"])
        self.assertEqual(result["template_sha256"], template_sha256(self.candidate))
        self.assertNotEqual(result["template_sha256"], self.baseline["template_sha256"])
        self.assertEqual(result["admitted_template_sha256"], result["template_sha256"])

    def test_restart_parameters_not_bound_to_this_incident_and_baseline_never_make_request(self):
        other = restart_token("INC-OTHER", self.baseline["baseline_id"])
        cases = {
            "unbound token": {"restart_token": "RST-" + "0" * 16},
            "token of another incident": {
                "restart_token": other,
                "template_sha256": template_sha256(restart_template(self.baseline["pod_template"], other))},
            "hash of the bare baseline": {"template_sha256": self.baseline["template_sha256"]},
            "hash of nothing reviewed": {"template_sha256": "c" * 64},
        }
        for name, change in cases.items():
            with self.subTest(case=name):
                self.setUp()
                self.proposal["parameters"].update(change)
                with patch("services.guard.preview.subprocess.run") as run:
                    result = self.preview()
                run.assert_not_called()
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["details_code"], "BASELINE_PRECONDITION_FAILED")

    def test_admission_that_drops_the_restart_token_is_a_mutation(self):
        self.response["spec"]["template"] = copy.deepcopy(self.baseline["pod_template"])
        with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
            result = self.preview()
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["details_code"], "ADMISSION_TEMPLATE_MUTATED")
        self.assertEqual(result["admitted_template_sha256"], self.baseline["template_sha256"])


if __name__ == "__main__":
    unittest.main()
