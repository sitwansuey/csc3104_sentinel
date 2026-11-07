"""Exercise the CLI boundary and its read-only Kubernetes adapter."""

import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.guard.core import proposal_fingerprint
from services.guard.adapters import (
    AdapterError, KUBECTL_TIMEOUT_SECONDS, load_json, read_deployment,
)
from services.guard.cli import main
from shared.contracts import validate_message


NOW = "2026-10-10T12:00:40Z"


def fixture(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


class GuardCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.values = {
            "proposal": self.proposal,
            "policy": self.policy,
            "baseline": fixture("data/sentinel/samples/baseline.json"),
            "dependencies": fixture("data/sentinel/samples/messages/dependency-result.json"),
            "live-state": self.live,
            "approval": {
                "action_id": self.proposal["action_id"],
                "proposal_sha256": proposal_fingerprint(self.proposal),
                "policy_version": self.policy["policy_version"],
                "operator_id": "fixture-only-operator",
                "decision": "APPROVED",
                "approved_at": "2026-10-10T12:00:35Z",
                "expires_at": "2026-10-10T12:01:35Z",
            },
        }
        for name, value in self.values.items():
            self.write(name, value)

    def path(self, name):
        return self.directory / f"{name}.json"

    def write(self, name, value):
        self.path(name).write_text(json.dumps(value), encoding="utf-8")

    def args(self, *, approval=True, live=True, now=True):
        names = ["proposal", "policy", "baseline", "dependencies"]
        if live:
            names.append("live-state")
        if approval:
            names.append("approval")
        result = [argument for name in names for argument in (f"--{name}", str(self.path(name)))]
        if now:
            result.extend(["--now", NOW])
        return result

    def run_cli(self, args=None):
        return subprocess.run(
            [sys.executable, "-m", "services.guard.cli", *(self.args() if args is None else args)],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=15,
        )

    def use_live_inputs(self):
        # Live adapters deliberately refuse the permissive development policy.
        # These synthetic inputs satisfy the policy shape; mocks still prevent
        # every actual Kubernetes call in these boundary tests.
        from experiments.validation.demo_guard_v2 import build_inputs
        values = build_inputs()
        self.proposal, self.policy, self.live = values["proposal"], values["policy"], values["live_state"]
        for name in self.values:
            self.values[name] = values["live_state" if name == "live-state" else name]
            self.write(name, self.values[name])

    def assert_error(self, process, code):
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(process.stdout, "")
        error = json.loads(process.stderr)
        self.assertEqual(error["kind"], "guard_error")
        self.assertEqual(error["error"]["code"], code)
        self.assertNotIn("Traceback", process.stderr)
        return error

    def test_arbitrary_file_inputs_allow_with_explicit_fixture_clock(self):
        result = self.run_cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        verdict = json.loads(result.stdout)
        validate_message(verdict)
        self.assertEqual(verdict["verdict"], "ALLOW")
        self.assertEqual(verdict["checked_at"], NOW)

    def test_stale_version_rejects_even_with_matching_approval(self):
        self.live["deployment"]["metadata"]["resourceVersion"] = "newer"
        self.write("live-state", self.live)
        process = self.run_cli()
        self.assertEqual(process.returncode, 2, process.stderr)
        verdict = json.loads(process.stdout)
        validate_message(verdict)
        self.assertEqual(verdict["verdict"], "REJECT")
        self.assertIn("STALE_RESOURCE_VERSION", verdict["reason_codes"])
        self.assertEqual(process.stderr, "")

    def test_missing_operator_approval_escalates(self):
        process = self.run_cli(self.args(approval=False))
        self.assertEqual(process.returncode, 3, process.stderr)
        verdict = json.loads(process.stdout)
        validate_message(verdict)
        self.assertEqual(verdict["verdict"], "ESCALATE")
        self.assertIn("OPERATOR_APPROVAL_REQUIRED", verdict["reason_codes"])

    def test_bad_json_encoding_duplicates_and_nonfinite_values_fail_closed(self):
        for data in (b'{"secret":', b'\xff\xfe', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}'):
            with self.subTest(data=data):
                self.path("policy").write_bytes(data)
                self.assert_error(self.run_cli(), "INVALID_JSON")

    def test_utf8_bom_files_work_on_windows(self):
        self.path("policy").write_text(json.dumps(self.policy), encoding="utf-8-sig")
        self.assertEqual(self.run_cli().returncode, 0)

    def test_numeric_resource_values_cannot_round_down_or_underflow_before_validation(self):
        for token in ("0.100000000000000000000001", "1e-9999", "134217728.000000001"):
            with self.subTest(token=token):
                self.path("baseline").write_text('{"quantity":' + token + '}', encoding="utf-8")
                self.assert_error(self.run_cli(), "INVALID_JSON")

    def test_missing_live_file_produces_error_without_a_verdict(self):
        self.path("live-state").unlink()
        error = self.assert_error(self.run_cli(), "INPUT_UNAVAILABLE")
        self.assertEqual(error["error"]["input"], "live_state")

    def test_unavailable_live_snapshot_produces_no_authorisation(self):
        for live in (None, {}, {"cluster_id": "example"}):
            with self.subTest(live=live):
                self.write("live-state", live)
                self.assert_error(self.run_cli(), "INVALID_GUARD_INPUT")

    def test_contract_error_does_not_echo_untrusted_keys(self):
        self.proposal["credentials-should-never-appear"] = "sensitive-value"
        self.write("proposal", self.proposal)
        process = self.run_cli()
        self.assert_error(process, "INVALID_GUARD_INPUT")
        self.assertNotIn("credentials-should-never-appear", process.stderr)
        self.assertNotIn("sensitive-value", process.stderr)

    def test_clock_must_be_explicit_utc(self):
        for clock in ("yesterday", "2026-10-10T12:00:40", "2026-10-10T20:00:40+08:00"):
            with self.subTest(clock=clock):
                self.assert_error(self.run_cli([*self.args(now=False), "--now", clock]), "ARGUMENT_ERROR")

    def test_live_mode_forbids_fixture_clock_before_running_kubectl(self):
        args = [*self.args(live=False), "--context", "team-context", "--cluster-id", "sentinel-lab"]
        with patch("services.guard.adapters.subprocess.run") as run:
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                code = main(args)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "ARGUMENT_ERROR")
            run.assert_not_called()

    def test_explicit_state_source_is_required_and_sources_cannot_be_combined(self):
        self.assert_error(self.run_cli(self.args(live=False)), "ARGUMENT_ERROR")
        self.assert_error(self.run_cli([*self.args(), "--context", "other"]), "ARGUMENT_ERROR")

    def test_live_context_requires_independent_cluster_id(self):
        args = [*self.args(live=False, now=False), "--context", "team-context"]
        self.assert_error(self.run_cli(args), "ARGUMENT_ERROR")

    def test_offline_mode_rejects_unused_cluster_id_and_kubeconfig(self):
        for option, value in (("--cluster-id", "cluster"), ("--kubeconfig", "credentials.yaml")):
            with self.subTest(option=option):
                self.assert_error(self.run_cli([*self.args(), option, value]), "ARGUMENT_ERROR")

    def test_default_clock_uses_actual_utc_for_file_inputs(self):
        before = datetime.now(timezone.utc)
        process = self.run_cli(self.args(approval=False, now=False))
        self.assertEqual(process.returncode, 3, process.stderr)
        captured = datetime.fromisoformat(json.loads(process.stdout)["checked_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, datetime.now(timezone.utc))

    def test_kubectl_failure_is_a_structured_error_without_stderr_leak(self):
        self.use_live_inputs()
        args = [*self.args(live=False, now=False), "--context", "team-context", "--cluster-id", "sentinel-lab"]
        failure = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"secret-token")
        with patch("services.guard.adapters.subprocess.run", return_value=failure):
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                    code = main(args)
        self.assertEqual(code, 1)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "LIVE_STATE_UNAVAILABLE")
        self.assertNotIn("secret-token", stderr.getvalue())

    def test_independent_live_cluster_label_rejects_mismatched_proposal(self):
        self.use_live_inputs()
        args = [*self.args(live=False, now=False, approval=False), "--context", "team-context", "--cluster-id", "different-cluster"]
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.live["deployment"]).encode(), b"")
        with patch("services.guard.adapters.subprocess.run", return_value=completed):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                    code = main(args)
        self.assertEqual(code, 2, stderr.getvalue())
        self.assertIn("LIVE_TARGET_MISMATCH", json.loads(stdout.getvalue())["reason_codes"])


class GuardAdapterTests(unittest.TestCase):
    def setUp(self):
        self.target = fixture("data/sentinel/samples/messages/proposal.json")["target"]
        self.deployment = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]["deployment"]

    def test_live_reader_binds_context_and_target_and_uses_only_get(self):
        before = datetime.now(timezone.utc)
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.deployment).encode(), b"")
        with patch("services.guard.adapters.subprocess.run", return_value=completed) as run:
            snapshot = read_deployment(self.target, context="explicit-team-context", cluster_id="independent-cluster-label")
        run.assert_called_once_with(
            ["kubectl", "--context=explicit-team-context", "--namespace=sentinel-demo",
             "--request-timeout=10s", "get", "deployment", "demo-api", "--output=json"],
            capture_output=True, check=False, timeout=KUBECTL_TIMEOUT_SECONDS,
        )
        self.assertEqual(snapshot["cluster_id"], "independent-cluster-label")
        self.assertEqual(snapshot["deployment"], self.deployment)
        captured = datetime.fromisoformat(snapshot["captured_at"].replace("Z", "+00:00"))
        self.assertLessEqual(before, captured)
        self.assertLessEqual(captured, datetime.now(timezone.utc))

    def test_private_kubeconfig_is_a_single_argument_without_environment_mutation(self):
        completed = subprocess.CompletedProcess([], 0, json.dumps(self.deployment).encode(), b"")
        with patch("services.guard.adapters.subprocess.run", return_value=completed) as run:
            read_deployment(self.target, context="team-context", cluster_id="sentinel-lab",
                            kubeconfig="C:/private config/team.yaml")
        command = run.call_args.args[0]
        self.assertIn("--kubeconfig=C:/private config/team.yaml", command)
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_missing_kubectl_and_timeout_fail_closed_without_diagnostics(self):
        for failure, code in (
            (FileNotFoundError("private-system-path"), "LIVE_STATE_UNAVAILABLE"),
            (subprocess.TimeoutExpired(["private-command"], 15, stderr=b"secret"), "LIVE_STATE_TIMEOUT"),
        ):
            with self.subTest(code=code):
                with patch("services.guard.adapters.subprocess.run", side_effect=failure):
                    with self.assertRaises(AdapterError) as caught:
                        read_deployment(self.target, context="team-context", cluster_id="sentinel-lab")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("private", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))

    def test_malformed_kubectl_json_and_encoding_are_rejected(self):
        for payload in (b"not json", b"\xff", b'{"x":NaN}'):
            with self.subTest(payload=payload):
                completed = subprocess.CompletedProcess([], 0, payload, b"")
                with patch("services.guard.adapters.subprocess.run", return_value=completed):
                    with self.assertRaises(AdapterError) as caught:
                        read_deployment(self.target, context="team-context", cluster_id="sentinel-lab")
                self.assertEqual(caught.exception.code, "INVALID_JSON")

    def test_empty_context_and_option_like_targets_never_invoke_kubectl(self):
        cases = [(self.target, ""), (self.target, " ")]
        for field in ("name", "namespace"):
            for value in ("--raw=/secret", "name with spaces", "bad..name"):
                changed = copy.deepcopy(self.target)
                changed[field] = value
                cases.append((changed, "team-context"))
        with patch("services.guard.adapters.subprocess.run") as run:
            for target, context in cases:
                with self.subTest(target=target, context=context):
                    with self.assertRaises(AdapterError):
                        read_deployment(target, context=context, cluster_id="sentinel-lab")
            run.assert_not_called()

    def test_empty_cluster_id_and_empty_kubeconfig_are_rejected(self):
        for cluster_id, kubeconfig in (("", None), (" ", None), ("sentinel-lab", "")):
            with self.subTest(cluster_id=cluster_id, kubeconfig=kubeconfig):
                with patch("services.guard.adapters.subprocess.run") as run:
                    with self.assertRaises(AdapterError):
                        read_deployment(self.target, context="team-context", cluster_id=cluster_id, kubeconfig=kubeconfig)
                    run.assert_not_called()

    def test_json_read_size_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.json"
            path.write_bytes(b" " * 101)
            with patch("services.guard.adapters.MAX_JSON_BYTES", 100):
                with self.assertRaises(AdapterError) as caught:
                    load_json(path, "policy")
        self.assertEqual(caught.exception.code, "INPUT_TOO_LARGE")


if __name__ == "__main__":
    unittest.main()
