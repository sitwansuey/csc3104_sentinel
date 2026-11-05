"""Offline live-runtime wiring tests: real Guard, fake API and synthetic authority.

The production adapter has separate subprocess-contract tests. Nothing in this
module contacts a cluster or establishes that a workload has recovered.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime.live_executor import LiveGuardAuthorizer, build_adapters, main
from scripts.demo_guard import load_fixtures
from services.executor import execute, reconcile
from tests.support.executor_fake import FakeDeploymentAPI
from services.guard import GuardInputError, proposal_fingerprint, template_sha256
from services.k8s_executor_adapter import KubectlDeploymentAPI as RealDeploymentAPI


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)
PRIVATE_MARKER = "private-kubeconfig-token-must-not-appear"


class SimulatedCrash(BaseException):
    pass


class LiveExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.fixtures = load_fixtures()
        self.proposal = self.fixtures["proposal"]
        self.policy = self.fixtures["policy"]
        self.baseline = self.fixtures["baseline"]
        self.snapshot = self.fixtures["live_state"]
        self.policy.update({
            "profile": "live", "decision_ttl_seconds": 20,
            "max_approval_lifetime_seconds": 60,
            "max_state_age_seconds": 30, "max_dependency_age_seconds": 30,
            "baseline_constraints": {
                "max_age_seconds": 300, "min_healthy_duration_seconds": 30,
                "trusted_sources": ["synthetic-verifier"], "require_health_evidence": True,
                "require_compatible_references": True, "require_image_digests": True,
            },
            "security_constraints": {
                "deny_privileged": True, "deny_host_access": True,
                "forbid_service_account_change": True,
                "allowed_image_registries": ["docker.io"],
                "allowed_service_accounts": ["default"],
            },
            "resource_constraints": {
                "require_requests_and_limits": True,
                "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
                "max_container_limits": {"cpu": "500m", "memory": "128Mi"},
            },
            "workload_constraints": {
                "max_pod_requests": {"cpu": "100m", "memory": "64Mi"},
                "max_rollout_requests": {"cpu": "1", "memory": "1Gi"},
                "max_replicas": 3, "max_surge_replicas": 1,
            },
        })
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = "demo/sentinel@sha256:" + "a" * 64
        self.baseline["reference_versions"] = []
        self.baseline["health_evidence"] = {
            "source": "synthetic-verifier", "evidence_id": "synthetic-window",
            "status": "HEALTHY", "target": deepcopy(self.proposal["target"]),
            "template_sha256": "pending", "window_start": "2026-10-10T11:58:30Z",
            "window_end": self.baseline["healthy_observed_at"],
        }
        self.approval = {
            "action_id": self.proposal["action_id"], "proposal_sha256": "pending",
            "policy_version": self.policy["policy_version"],
            "operator_id": "synthetic-operator", "decision": "APPROVED",
            "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
        }
        self.bind_template()
        self.paths = {name: self.directory / (name + ".json")
                      for name in ("proposal", "policy", "baseline", "dependencies", "approval", "allowed_targets")}
        self.save_inputs()
        self.kubeconfig = self.directory / "restricted.kubeconfig"
        self.kubeconfig.write_text(PRIVATE_MARKER, encoding="utf-8")
        self.cluster = FakeDeploymentAPI(self.snapshot, clock=lambda: NOW)
        self.factory = patch("services.k8s_executor_adapter.KubectlDeploymentAPI", return_value=self.cluster).start()
        self.addCleanup(patch.stopall)
        self.references = patch("runtime.live_executor.read_reference_versions", return_value=[]).start()
        patch("runtime.live_executor._utc_now", return_value=NOW).start()

    def bind_template(self):
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = digest
        self.baseline["health_evidence"]["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)

    def save_inputs(self):
        values = dict(proposal=self.proposal, policy=self.policy, baseline=self.baseline,
                      dependencies=self.fixtures["dependencies"], approval=self.approval,
                      allowed_targets=[self.proposal["target"]])
        for name, value in values.items():
            self.paths[name].write_text(json.dumps(value), encoding="utf-8")

    def build(self, **changes):
        values = dict(context="restricted-lab", cluster_id="sentinel-lab",
                      kubeconfig=self.kubeconfig, allowed_targets=self.paths["allowed_targets"],
                      ledger=self.directory / "executor.db", locks=self.directory / "locks",
                      **{key: self.paths[key] for key in ("policy", "baseline", "dependencies", "approval")})
        values.update(changes)
        adapters = build_adapters(**values)
        self.addCleanup(adapters.ledger.close)
        return adapters

    def authorizer(self, **changes):
        values = dict(context="restricted-lab", cluster_id="sentinel-lab", kubeconfig=self.kubeconfig,
                      **{key: self.paths[key] for key in ("policy", "baseline", "dependencies", "approval")})
        values.update(changes)
        return LiveGuardAuthorizer(**values)

    def arguments(self, *, reconciliation=False):
        args = ["--proposal", str(self.paths["proposal"]), "--context", "restricted-lab",
                "--cluster-id", "sentinel-lab", "--kubeconfig", str(self.kubeconfig),
                "--allowed-targets", str(self.paths["allowed_targets"]),
                "--ledger", str(self.directory / "executor.db"),
                "--locks", str(self.directory / "locks")]
        if reconciliation:
            args.append("--reconcile")
        else:
            for key in ("policy", "baseline", "dependencies", "approval"):
                args.extend(["--" + key, str(self.paths[key])])
        return args

    def call_cli(self, arguments):
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = main(arguments)
        self.assertNotIn(PRIVATE_MARKER, stdout.getvalue() + stderr.getvalue())
        return status, stdout.getvalue(), stderr.getvalue()

    def test_strict_live_policy_is_required_before_client_or_api_access(self):
        self.policy["profile"] = "development"
        self.save_inputs()
        with self.assertRaises(GuardInputError):
            self.build()
        self.factory.assert_not_called()
        self.references.assert_not_called()
        self.assertFalse((self.directory / "executor.db").exists())

    def test_new_execution_requires_all_authoritative_files(self):
        with self.assertRaises(ValueError):
            self.build(approval=None)
        self.factory.assert_not_called()
        self.assertEqual(self.cluster.write_attempts, [])

    def test_real_guard_augments_the_same_snapshot_with_reference_metadata(self):
        reference = {"kind": "ConfigMap", "namespace": self.proposal["target"]["namespace"],
                     "name": "runtime-config", "uid": "config-uid", "resource_version": "config-rv"}
        self.baseline["pod_template"]["spec"]["containers"][0]["env"].append({
            "name": "CONFIG", "valueFrom": {"configMapKeyRef": {"name": "runtime-config", "key": "MODE"}},
        })
        self.baseline["reference_versions"] = [reference]
        self.bind_template()
        self.save_inputs()
        self.references.return_value = [reference]
        before = deepcopy(self.snapshot)
        check = self.authorizer().validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW", check.decision)
        self.assertEqual(self.snapshot, before)
        self.assertEqual(check.inputs["live_state"]["deployment"], before["deployment"])
        self.assertEqual(check.inputs["live_state"]["captured_at"], before["captured_at"])
        self.assertEqual(check.inputs["live_state"]["reference_versions"], [reference])
        self.assertEqual(self.references.call_args.args[0], self.proposal["target"])
        self.assertEqual(self.references.call_args.kwargs["kubeconfig"], self.kubeconfig)
        self.factory.assert_not_called()

    def test_authorization_files_are_reloaded_for_each_validation(self):
        authorizer = self.authorizer()
        first = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(first.decision["verdict"], "ALLOW")
        self.approval["decision"] = "REJECTED"
        self.save_inputs()
        second = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(second.decision["verdict"], "REJECT")
        self.assertIn("OPERATOR_REJECTED", second.decision["reason_codes"])

    def test_fresh_dependency_reader_overrides_previously_clear_file_before_mutation(self):
        adapters = self.build()
        dependencies = deepcopy(self.fixtures["dependencies"])
        dependencies.update(status="BLOCKED", blockers=["dependency changed after proposal"])
        adapters.authorizer.dependency_reader = lambda proposal: deepcopy(dependencies)
        self.assertEqual(json.loads(self.paths["dependencies"].read_text())["status"], "CLEAR")
        result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("DEPENDENCY_BLOCKED", result["details"])
        self.assertEqual(self.cluster.writes, [])

    def test_injected_coordinator_approval_is_fresh_and_missing_record_cannot_authorize(self):
        records = [deepcopy(self.approval)]
        calls = []

        def approval_reader(proposal):
            calls.append(proposal["action_id"])
            return deepcopy(records[0])

        authorizer = self.authorizer(approval=None, approval_reader=approval_reader)
        check = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW")
        records[0] = None
        with self.assertRaises(GuardInputError):
            authorizer.assert_usable(check, now=NOW)
        withdrawn = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(withdrawn.decision["verdict"], "ESCALATE")
        self.assertIn("OPERATOR_APPROVAL_REQUIRED", withdrawn.decision["reason_codes"])
        self.assertEqual(calls, [self.proposal["action_id"]] * 3)

    def test_build_can_use_coordinator_approval_without_an_approval_file(self):
        adapters = self.build(approval=None, approval_reader=lambda proposal: deepcopy(self.approval))
        self.assertEqual(execute(self.proposal, adapters)["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)

    def test_independent_execution_scope_cannot_be_replaced_by_proposal_target(self):
        allowed = deepcopy(self.proposal["target"])
        allowed["name"] = "another-deployment"
        self.paths["allowed_targets"].write_text(json.dumps([allowed]), encoding="utf-8")
        self.factory.side_effect = RealDeploymentAPI
        calls = []

        def forbidden_runner(*args, **kwargs):
            calls.append(args)
            raise AssertionError("Out-of-scope target must never reach kubectl")

        adapters = self.build(runner=forbidden_runner)
        self.assertEqual(execute(self.proposal, adapters)["status"], "FAILED")
        self.assertEqual(calls, [])

    def test_optional_preview_reuses_snapshot_and_real_guard_and_fails_closed(self):
        self.policy.update(require_server_dry_run=True, max_server_dry_run_age_seconds=10)
        self.save_inputs()
        unavailable = self.authorizer().validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(unavailable.decision["verdict"], "ESCALATE")
        preview = {
            "status": "PASSED", "proposal_sha256": proposal_fingerprint(self.proposal),
            "target": deepcopy(self.proposal["target"]),
            "resource_version": self.proposal["based_on_resource_version"],
            "template_sha256": self.baseline["template_sha256"],
            "admitted_template_sha256": self.baseline["template_sha256"],
            "checked_at": NOW.isoformat(), "details_code": "SYNTHETIC_ADMISSION",
        }
        with patch("runtime.live_executor.collect_server_preview", return_value=preview) as collect:
            check = self.authorizer(server_dry_run=True).validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW", check.decision)
        self.assertEqual(collect.call_args.args[1], check.inputs["live_state"])
        self.assertEqual(collect.call_args.kwargs["kubeconfig"], self.kubeconfig)

    def test_hard_rejection_never_runs_even_a_preview_patch(self):
        self.policy["targets"] = []
        self.save_inputs()
        with patch("runtime.live_executor.collect_server_preview") as collect:
            check = self.authorizer(server_dry_run=True).validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "REJECT")
        collect.assert_not_called()

    def test_reconciliation_after_restart_needs_no_authorization_files(self):
        class CrashAfterCommit(FakeDeploymentAPI):
            def conditional_restore(api, **kwargs):
                super().conditional_restore(**kwargs)
                raise SimulatedCrash()
        cluster = CrashAfterCommit(self.snapshot, clock=lambda: NOW)
        self.factory.return_value = cluster
        adapters = self.build()
        with self.assertRaises(SimulatedCrash):
            execute(self.proposal, adapters)
        adapters.ledger.close()
        for key in ("policy", "baseline", "dependencies", "approval"):
            self.paths[key].unlink()
        restarted = self.build(reconcile_only=True, policy=None, baseline=None,
                               dependencies=None, approval=None)
        result = reconcile(self.proposal, restarted)
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(len(cluster.writes), 1)

    def test_cli_emits_only_result_and_never_echoes_credentials(self):
        status, stdout, stderr = self.call_cli(self.arguments())
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "APPLIED")
        self.assertEqual(stderr, "")
        self.assertEqual(len(self.cluster.writes), 1)

    def test_cli_reconcile_cannot_start_an_unjournaled_action(self):
        status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True))
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["kind"], "executor_error")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_cli_refuses_fixture_clock_override_without_echoing_unknown_arguments(self):
        status, stdout, stderr = self.call_cli(self.arguments() + ["--now", PRIVATE_MARKER])
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
        self.factory.assert_not_called()

    def test_cli_hides_raw_adapter_error_text(self):
        with patch("runtime.live_executor.build_adapters", side_effect=RuntimeError(PRIVATE_MARKER)):
            status, stdout, stderr = self.call_cli(self.arguments())
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "EXECUTOR_ERROR")

    def test_ledger_and_sidecar_aliases_cannot_overwrite_inputs_or_credentials(self):
        original = self.kubeconfig.read_bytes()
        for destination in (self.kubeconfig, self.paths["policy"], self.paths["allowed_targets"]):
            with self.subTest(destination=destination.name), self.assertRaises(ValueError):
                self.build(ledger=destination)
        sidecar = Path(str(self.directory / "executor.db") + "-wal")
        sidecar.write_bytes(original)
        with self.assertRaises(ValueError):
            self.build(kubeconfig=sidecar)
        self.assertEqual(self.kubeconfig.read_bytes(), original)
        self.assertEqual(sidecar.read_bytes(), original)
        self.factory.assert_not_called()

    def test_existing_hard_link_alias_of_credentials_is_rejected(self):
        alias = self.directory / "credential-alias.db"
        os.link(self.kubeconfig, alias)
        with self.assertRaises(ValueError):
            self.build(ledger=alias)
        self.assertEqual(self.kubeconfig.read_text(encoding="utf-8"), PRIVATE_MARKER)
        self.factory.assert_not_called()

    def test_cli_ledger_alias_of_proposal_is_rejected_before_modifying_it(self):
        original = self.paths["proposal"].read_bytes()
        arguments = self.arguments()
        arguments[arguments.index("--ledger") + 1] = str(self.paths["proposal"])
        status, stdout, stderr = self.call_cli(arguments)
        self.assertEqual(status, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
        self.assertEqual(self.paths["proposal"].read_bytes(), original)
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
