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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime.live_executor import LiveGuardAuthorizer, build_adapters, main
from experiments.validation.demo_guard import load_fixtures
from services.executor.core import execute, reconcile
from tests.support.executor_fake import FakeDeploymentAPI
from services.guard.core import GuardInputError, proposal_fingerprint, template_sha256
from services.executor.kubernetes_adapter import KubectlDeploymentAPI as RealDeploymentAPI


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)
PRIVATE_MARKER = "private-kubeconfig-token-must-not-appear"
READER_MARKER = "guard-reader-kubeconfig-token-must-not-appear"
GATE_SCANNER = "aquasec/trivy@sha256:" + "f" * 64


def gate_scan(image, *findings):
    """A trusted, fresh scan summary for the CVE gate."""
    return {"image": image, "status": "FINDINGS" if findings else "NO_KNOWN_CVES", "scanner_image": GATE_SCANNER,
            "scanned_at": "2026-10-10T11:00:00Z", "findings": [{"id": cve, "severity": level} for cve, level in findings]}


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
        self.reader = self.directory / "guard-reader.kubeconfig"
        self.reader.write_text(READER_MARKER, encoding="utf-8")
        self.cluster = FakeDeploymentAPI(self.snapshot, clock=lambda: NOW)
        self.factory = patch("services.executor.kubernetes_adapter.KubectlDeploymentAPI", return_value=self.cluster).start()
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
        self.assertNotIn(READER_MARKER, stdout.getvalue() + stderr.getvalue())
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
        check = self.authorizer(reference_kubeconfig=self.reader,
                                reference_context="guard-reader-lab").validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW", check.decision)
        self.assertEqual(self.snapshot, before)
        self.assertEqual(check.inputs["live_state"]["deployment"], before["deployment"])
        self.assertEqual(check.inputs["live_state"]["captured_at"], before["captured_at"])
        self.assertEqual(check.inputs["live_state"]["reference_versions"], [reference])
        self.assertEqual(self.references.call_args.args[0], self.proposal["target"])
        # P5.05 (#69): the reads use the Guard-reader identity, never the Executor's kubeconfig.
        self.assertEqual(self.references.call_args.kwargs["kubeconfig"], self.reader)
        self.assertEqual(self.references.call_args.kwargs["context"], "guard-reader-lab")
        self.factory.assert_not_called()

    # P5.05: the Guard-reader identity is split from the Executor's (#69, live_executor.py LiveGuardAuthorizer).

    def add_config_reference(self):
        """A ConfigMap and a Secret reference in the baseline, with their saved versions; returns the records."""
        namespace = self.proposal["target"]["namespace"]
        records = [{"kind": kind, "namespace": namespace, "name": "security-upstream", "uid": kind + "-uid",
                    "resource_version": "7"} for kind in ("ConfigMap", "Secret")]
        spec = self.baseline["pod_template"]["spec"]
        spec["volumes"] = [{"name": "config", "configMap": {"name": "security-upstream"}},
                           {"name": "keys", "secret": {"secretName": "security-upstream"}}]
        self.baseline["reference_versions"] = deepcopy(records)
        self.bind_template()
        self.save_inputs()
        self.references.return_value = deepcopy(records)
        return records

    def test_reference_reads_never_use_the_executor_kubeconfig(self):
        self.add_config_reference()
        check = self.authorizer().validate(self.proposal, self.snapshot, now=NOW)
        self.references.assert_not_called()
        self.assertNotIn("reference_versions", check.inputs["live_state"])
        self.assertEqual(check.decision["verdict"], "ESCALATE")
        self.assertIn("BASELINE_REFERENCE_EVIDENCE_MISSING", check.decision["reason_codes"])

    def test_a_missing_reader_escalates_and_writes_nothing(self):
        self.add_config_reference()
        result = execute(self.proposal, self.build())
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("BASELINE_REFERENCE_EVIDENCE_MISSING", result["details"])
        self.assertEqual(self.cluster.write_attempts, [])
        self.references.assert_not_called()

    def test_the_reader_identity_authorizes_one_write(self):
        self.add_config_reference()
        result = execute(self.proposal, self.build(reference_kubeconfig=self.reader, reference_context="guard-reader-lab"))
        self.assertEqual(result["status"], "APPLIED", result)
        self.assertEqual(len(self.cluster.writes), 1)
        self.assertTrue(self.references.call_args_list)
        for call in self.references.call_args_list:
            self.assertEqual((call.kwargs["kubeconfig"], call.kwargs["context"]), (self.reader, "guard-reader-lab"))

    def test_the_executor_credential_never_reads_a_secret(self):
        """With the real reference adapter, every kubectl get of a Secret or ConfigMap names the reader only."""
        from services.guard.adapters import read_reference_versions as real_reader
        self.add_config_reference()
        self.references.side_effect = real_reader
        commands = []

        versions = {"configmap": b"ConfigMap-uid\t7", "secret": b"Secret-uid\t7"}

        def kubectl(command, **kwargs):
            commands.append(command)
            return SimpleNamespace(returncode=0, stdout=versions[command[command.index("get") + 1]])

        with patch("services.guard.adapters.subprocess.run", side_effect=kubectl):
            result = execute(self.proposal, self.build(reference_kubeconfig=self.reader,
                                                       reference_context="guard-reader-lab"))
        self.assertEqual(result["status"], "APPLIED", result)
        reads = [command for command in commands if "get" in command]
        self.assertEqual(sorted(command[command.index("get") + 1] for command in reads), ["configmap", "secret"])
        for command in reads:
            self.assertIn("--kubeconfig=" + str(self.reader), command)
            self.assertIn("--context=guard-reader-lab", command)
            self.assertNotIn("--kubeconfig=" + str(self.kubeconfig), command)

    def test_a_template_without_references_needs_no_reader(self):
        check = self.authorizer().validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW", check.decision)
        self.assertEqual(check.inputs["live_state"]["reference_versions"], [])
        self.references.assert_not_called()

    def test_reference_versions_already_in_the_snapshot_are_never_trusted(self):
        records = self.add_config_reference()
        snapshot = dict(deepcopy(self.snapshot), reference_versions=records)
        check = self.authorizer().validate(self.proposal, snapshot, now=NOW)
        self.assertNotIn("reference_versions", check.inputs["live_state"])
        self.assertEqual(check.decision["verdict"], "ESCALATE")

    # P5.06: a Secret owned by a reviewed cert-manager Certificate is never read; its Certificate status is.

    def add_certificate_identity(self, **status):
        """The add_config_reference template with its Secret owned by a reviewed Certificate; returns its record."""
        self.add_config_reference()
        namespace = self.proposal["target"]["namespace"]
        issuer = {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": "sentinel-lab-ca"}
        self.policy["security_constraints"]["certificate_identities"] = [{
            "secret": "security-upstream", "certificate": "security-upstream", "issuer": issuer,
            "dns_names": ["upstream.internal"], "min_remaining_seconds": 1800}]
        record = {"kind": "Certificate", "namespace": namespace, "name": "security-upstream", "uid": "cert-uid",
                  "generation": 1, "secret_name": "security-upstream", "dns_names": ["upstream.internal"],
                  "issuer": issuer, "ready": "True", "ready_observed_generation": 1,
                  "not_before": "2026-10-10T11:00:00Z", "not_after": "2026-10-10T13:00:00Z", "revision": 2}
        self.baseline["reference_versions"] = [item for item in self.baseline["reference_versions"]
                                               if item["kind"] != "Secret"]
        self.baseline["certificates"] = [deepcopy(record)]
        self.save_inputs()
        self.references.return_value = deepcopy(self.baseline["reference_versions"])
        self.certificates = patch("runtime.live_executor.read_certificate_status",
                                  return_value=[dict(deepcopy(record), **status)]).start()
        return record

    def test_an_owned_secret_is_read_through_its_certificate_by_the_reader_only(self):
        self.add_certificate_identity(revision=3)  # renewed since the baseline: still the same identity
        result = execute(self.proposal, self.build(reference_kubeconfig=self.reader, reference_context="guard-reader-lab"))
        self.assertEqual(result["status"], "APPLIED", result)
        self.assertEqual(len(self.cluster.writes), 1)
        for call in self.references.call_args_list:
            self.assertEqual({item["kind"] for item in call.args[1]}, {"ConfigMap"})
        self.assertTrue(self.certificates.call_args_list)
        for call in self.certificates.call_args_list:
            self.assertEqual(call.args[1], ["security-upstream"])
            self.assertEqual((call.kwargs["kubeconfig"], call.kwargs["context"]), (self.reader, "guard-reader-lab"))

    def test_an_expiring_or_not_ready_certificate_escalates_and_writes_nothing(self):
        for status, code in (({"not_after": "2026-10-10T12:10:00Z"}, "SECURITY_IDENTITY_EXPIRING"),
                             ({"ready": "False"}, "SECURITY_IDENTITY_NOT_READY")):
            with self.subTest(code=code):
                self.add_certificate_identity(**status)
                check = self.authorizer(reference_kubeconfig=self.reader).validate(self.proposal, self.snapshot, now=NOW)
                self.assertEqual(check.decision["verdict"], "ESCALATE")
                self.assertIn(code, check.decision["reason_codes"])
                result = execute(self.proposal, self.build(reference_kubeconfig=self.reader,
                                                           ledger=self.directory / (code + ".db")))
                self.assertEqual(result["status"], "FAILED")
                self.assertIn(code, result["details"])
                self.assertEqual(self.cluster.write_attempts, [])

    def test_certificate_status_already_in_the_snapshot_is_never_trusted(self):
        record = self.add_certificate_identity()
        snapshot = dict(deepcopy(self.snapshot), certificates=[record])
        check = self.authorizer().validate(self.proposal, snapshot, now=NOW)
        self.assertNotIn("certificates", check.inputs["live_state"])
        self.assertEqual(check.decision["verdict"], "ESCALATE")
        self.certificates.assert_not_called()

    def test_with_the_real_adapters_no_kubectl_call_names_a_secret(self):
        from services.guard.adapters import read_certificate_status as real_status, read_reference_versions as real_reader
        record = self.add_certificate_identity()
        self.references.side_effect = real_reader
        self.certificates.side_effect = real_status
        commands = []
        certificate = {"apiVersion": "cert-manager.io/v1", "kind": "Certificate",
                       "metadata": {"name": record["name"], "namespace": record["namespace"], "uid": record["uid"],
                                    "generation": 1},
                       "spec": {"secretName": "security-upstream", "dnsNames": ["upstream.internal"],
                                "issuerRef": record["issuer"]},
                       "status": {"conditions": [{"type": "Ready", "status": "True", "observedGeneration": 1}],
                                  "notBefore": record["not_before"], "notAfter": record["not_after"], "revision": 2}}

        def kubectl(command, **kwargs):
            commands.append(command)
            kind = command[command.index("get") + 1]
            return SimpleNamespace(returncode=0, stdout=json.dumps(certificate).encode()
                                   if kind == "certificates.cert-manager.io" else b"ConfigMap-uid\t7")

        with patch("services.guard.adapters.subprocess.run", side_effect=kubectl):
            result = execute(self.proposal, self.build(reference_kubeconfig=self.reader,
                                                       reference_context="guard-reader-lab"))
        self.assertEqual(result["status"], "APPLIED", result)
        reads = [command for command in commands if "get" in command]
        self.assertEqual(sorted({command[command.index("get") + 1] for command in reads}),
                         ["certificates.cert-manager.io", "configmap"])
        self.assertFalse([command for command in reads if "secret" in command[command.index("get") + 1]])
        for command in reads:
            self.assertIn("--kubeconfig=" + str(self.reader), command)

    def test_the_reader_must_be_a_separate_credential_file(self):
        alias = self.directory / "reader-alias.kubeconfig"
        os.link(self.kubeconfig, alias)
        for reader in (self.kubeconfig, alias, " "):
            with self.subTest(reader=str(reader)), self.assertRaises(ValueError):
                self.build(reference_kubeconfig=reader)
        with self.assertRaises(ValueError):
            self.build(reference_kubeconfig=self.reader, ledger=self.reader)
        self.assertEqual(self.reader.read_text(encoding="utf-8"), READER_MARKER)
        self.factory.assert_not_called()

    def test_the_authorizer_itself_refuses_the_executor_kubeconfig_as_reader(self):
        # worker_service builds the Guard role's authorizer without build_adapters.
        from contextlib import ExitStack
        from runtime import worker_service
        alias = self.directory / "reader-alias.kubeconfig"
        os.link(self.kubeconfig, alias)
        for reader in (self.kubeconfig, alias, " "):
            with self.subTest(reader=str(reader)), self.assertRaises(ValueError):
                self.authorizer(reference_kubeconfig=reader)
        config = {"context": "restricted-lab", "cluster_id": "sentinel-lab", "kubeconfig": str(self.kubeconfig),
                  "policy": "p", "baseline": "b", "remote_dependencies": False, "role": "guard",
                  "reference_kubeconfig": str(alias), "reference_context": "guard-reader-lab",
                  "approvals": {"url": "https://127.0.0.1:1", "identity": "worker-1", "token_env": "TEST_TOKEN"}}
        with patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}), patch("runtime.approval_http.ApprovalClient"), \
                ExitStack() as stack, self.assertRaisesRegex(ValueError, "separate from the Executor"):
            worker_service.adapter(config, self.directory, stack)
        self.assertEqual(self.authorizer(reference_kubeconfig=self.reader).reference_kubeconfig, self.reader)

    def test_cli_passes_the_reader_identity(self):
        with patch("runtime.live_executor.build_adapters", side_effect=GuardInputError("stop")) as build:
            self.call_cli(self.arguments() + ["--reference-kubeconfig", str(self.reader),
                                              "--reference-context", "guard-reader-lab"])
        self.assertEqual(build.call_args.kwargs["reference_kubeconfig"], str(self.reader))
        self.assertEqual(build.call_args.kwargs["reference_context"], "guard-reader-lab")
        with patch("runtime.live_executor.build_adapters", side_effect=GuardInputError("stop")) as build:
            self.call_cli(self.arguments())
        self.assertIsNone(build.call_args.kwargs["reference_kubeconfig"])

    def test_cli_ledger_alias_of_the_reader_is_refused(self):
        arguments = self.arguments() + ["--reference-kubeconfig", str(self.reader)]
        arguments[arguments.index("--ledger") + 1] = str(self.reader)
        status, stdout, stderr = self.call_cli(arguments)
        self.assertEqual((status, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
        self.assertEqual(self.reader.read_text(encoding="utf-8"), READER_MARKER)

    def test_worker_service_gives_both_guarded_roles_the_reader_identity(self):
        from contextlib import ExitStack
        from runtime import worker_service
        config = {"context": "restricted-lab", "cluster_id": "sentinel-lab", "kubeconfig": "k",
                  "policy": "p", "baseline": "b", "allowed_targets": "t", "remote_dependencies": False,
                  "reference_kubeconfig": "reader", "reference_context": "guard-reader-lab",
                  "approvals": {"url": "https://127.0.0.1:1", "identity": "worker-1", "token_env": "TEST_TOKEN"}}
        with patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}), patch("runtime.approval_http.ApprovalClient"), \
                patch("runtime.live_executor.build_adapters") as build, ExitStack() as stack:
            worker_service.adapter(dict(config, role="executor"), self.directory, stack)
            self.assertEqual((build.call_args.kwargs["reference_kubeconfig"], build.call_args.kwargs["reference_context"]),
                             ("reader", "guard-reader-lab"))
            guard = worker_service.adapter(dict(config, role="guard"), self.directory, stack)
        self.assertEqual((guard.authorizer.reference_kubeconfig, guard.authorizer.reference_context),
                         ("reader", "guard-reader-lab"))
        self.assertEqual(guard.authorizer.kubeconfig, "k")

    def test_authorization_files_are_reloaded_for_each_validation(self):
        authorizer = self.authorizer()
        first = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(first.decision["verdict"], "ALLOW")
        self.approval["decision"] = "REJECTED"
        self.save_inputs()
        second = authorizer.validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(second.decision["verdict"], "REJECT")
        self.assertIn("OPERATOR_REJECTED", second.decision["reason_codes"])

    def test_the_guard_re_check_reads_this_hosts_clock_not_the_callers_now(self):
        # A host 300 s behind sees the approval and evidence stamped by others in its future,
        # whatever now the caller passes; a host 300 s ahead sees them stale. Both refuse.
        for offset in (0, -300, 300):
            with self.subTest(offset=offset):
                host = NOW + timedelta(seconds=offset)
                snapshot = dict(self.snapshot, captured_at=host.isoformat().replace("+00:00", "Z"))
                check = self.authorizer(clock=lambda host=host: host).validate(self.proposal, snapshot, now=NOW)
                self.assertEqual(check.decision["verdict"] == "ALLOW", offset == 0, check.decision["reason_codes"])
        adapters = self.build(clock=lambda: NOW - timedelta(seconds=300))
        self.assertEqual(adapters.authorizer.clock(), NOW - timedelta(seconds=300))
        self.assertEqual(adapters.clock(), NOW - timedelta(seconds=300))
        result = execute(self.proposal, adapters)
        self.assertEqual((result["status"], self.cluster.write_attempts), ("FAILED", []))

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

    def enable_cve_gate(self):
        """P5.02: a non-regression CVE gate, and a current template that runs the baseline's digest."""
        image = self.baseline["pod_template"]["spec"]["containers"][0]["image"]
        self.snapshot["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = image
        self.policy["security_constraints"]["image_vulnerability_gate"] = {
            "block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression", "max_scan_age_seconds": 86400,
            "trusted_scanner_images": [GATE_SCANNER]}
        self.save_inputs()
        self.cluster.snapshot = deepcopy(self.snapshot)
        return image

    def test_cve_gate_scans_cover_every_image_of_both_templates(self):
        image = self.enable_cve_gate()
        self.baseline["pod_template"]["spec"]["initContainers"] = [
            {"name": "setup", "image": image, "resources": deepcopy(
                self.baseline["pod_template"]["spec"]["containers"][0]["resources"]),
             "securityContext": {"allowPrivilegeEscalation": False}}]
        self.bind_template()
        self.save_inputs()
        current_init = "demo/sentinel@sha256:" + "c" * 64
        self.snapshot["deployment"]["spec"]["template"]["spec"]["initContainers"] = [
            dict(deepcopy(self.baseline["pod_template"]["spec"]["initContainers"][0]), image=current_init)]
        requested = []
        scans = {"scans": [gate_scan(image, ("CVE-1", "HIGH")), gate_scan(current_init)], "exceptions": []}

        def reader(images):
            requested.append(images)
            return deepcopy(scans)

        check = self.authorizer(scan_reader=reader).validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(requested, [sorted({image, current_init})])
        self.assertEqual(check.inputs["live_state"]["image_scans"], scans)
        self.assertNotIn("image_scans", self.snapshot)
        # Containers are compared by name: the init container's High is new against the current clean init image.
        self.assertEqual((check.decision["verdict"], check.decision["reason_codes"]), ("REJECT", ["IMAGE_CVE_REGRESSION"]))
        scans["scans"][1] = gate_scan(current_init, ("CVE-1", "HIGH"))
        check = self.authorizer(scan_reader=reader).validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ALLOW", check.decision["reason_codes"])

    def test_cve_gate_reader_failure_escalates_never_allows(self):
        self.enable_cve_gate()
        for error in (OSError("disk"), ValueError("bad file")):
            with self.subTest(error=error):
                def reader(images, error=error):
                    raise error
                check = self.authorizer(scan_reader=reader).validate(self.proposal, self.snapshot, now=NOW)
                self.assertEqual(check.decision["verdict"], "ESCALATE")
                self.assertIn("IMAGE_SCANS_MISSING", check.decision["reason_codes"])
                self.assertNotIn("image_scans", check.inputs["live_state"])

    def test_cve_gate_ignores_scans_that_did_not_come_from_the_trusted_reader(self):
        image = self.enable_cve_gate()
        self.snapshot["image_scans"] = {"scans": [gate_scan(image)], "exceptions": []}
        check = self.authorizer().validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "ESCALATE")
        self.assertIn("IMAGE_SCANS_MISSING", check.decision["reason_codes"])

    def test_cve_gate_reads_a_scan_directory_and_blocks_a_worse_digest(self):
        image = self.enable_cve_gate()
        scans = self.directory / "scans"
        scans.mkdir()
        (scans / "current.json").write_text(json.dumps(gate_scan(image, ("CVE-1", "HIGH"))), encoding="utf-8")
        exceptions = self.directory / "cve-exceptions.json"
        exceptions.write_text(json.dumps({"schema": "sentinel-cve-exceptions-v1", "exceptions": []}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build(cve_exceptions=exceptions)
        adapters = self.build(image_scans=scans, cve_exceptions=exceptions)
        self.assertEqual(execute(self.proposal, adapters)["status"], "APPLIED")
        # A crafted baseline with a different, more vulnerable digest: REJECT, no write.
        worse = "demo/sentinel@sha256:" + "d" * 64
        (scans / "worse.json").write_text(json.dumps(gate_scan(worse, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL"))),
                                          encoding="utf-8")
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = worse
        self.bind_template()
        self.save_inputs()
        check = self.authorizer(scan_reader=adapters.authorizer.scan_reader).validate(self.proposal, self.snapshot, now=NOW)
        self.assertEqual(check.decision["verdict"], "REJECT")
        self.assertIn("IMAGE_CVE_REGRESSION", check.decision["reason_codes"])

    def test_cve_gate_regression_is_not_applied_and_writes_nothing(self):
        image = self.enable_cve_gate()
        worse = "demo/sentinel@sha256:" + "d" * 64
        scans = self.directory / "scans"
        scans.mkdir()
        (scans / "current.json").write_text(json.dumps(gate_scan(image, ("CVE-1", "HIGH"))), encoding="utf-8")
        (scans / "worse.json").write_text(json.dumps(gate_scan(worse, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL"))),
                                          encoding="utf-8")
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = worse
        self.bind_template()
        self.save_inputs()
        result = execute(self.proposal, self.build(image_scans=scans))
        self.assertNotEqual(result["status"], "APPLIED")
        self.assertIn("IMAGE_CVE_REGRESSION", json.dumps(result))
        self.assertEqual(self.cluster.writes, [])

    def test_cli_passes_the_scan_directory_and_exceptions(self):
        scans, exceptions = self.directory / "scans", self.directory / "cve-exceptions.json"
        with patch("runtime.live_executor.build_adapters", side_effect=GuardInputError("stop")) as build:
            self.call_cli(self.arguments() + ["--image-scans", str(scans), "--cve-exceptions", str(exceptions)])
        self.assertEqual(build.call_args.kwargs["image_scans"], str(scans))
        self.assertEqual(build.call_args.kwargs["cve_exceptions"], str(exceptions))

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
        # Stateless reconcile: unchanged live state stays in doubt (exit 4), never a write.
        status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True))
        self.assertEqual(status, 4, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertTrue(result["details"].startswith("[basis=UNCHANGED_IN_DOUBT]"))
        self.assertEqual(self.cluster.write_attempts, [])
        # The adopted journal row means a later execution can only reconcile.
        status, stdout, stderr = self.call_cli(self.arguments())
        self.assertEqual(status, 4, stderr)
        self.assertEqual(self.cluster.write_attempts, [])

    def test_cli_fence_after_resolves_an_in_doubt_write_with_a_metadata_fence(self):
        status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True))
        self.assertEqual(status, 4, stderr)
        self.assertEqual(self.cluster.fence_attempts, [])  # Fencing is off unless configured.
        status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True) + ["--fence-after", "0"])
        self.assertEqual(status, 3, stderr)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "CONFLICT")
        self.assertTrue(result["details"].startswith("[basis=FENCED]"))
        self.assertEqual((len(self.cluster.fences), self.cluster.write_attempts), (1, []))

    def test_cli_refuses_an_invalid_fence_delay_before_any_api_call(self):
        for value in ("-1", "inf", "nan", "soon"):
            with self.subTest(value=value):
                status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True) + ["--fence-after", value])
                self.assertEqual((status, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
        self.factory.assert_not_called()

    def test_build_adapters_passes_the_trusted_fence_delay(self):
        self.assertIsNone(self.build().fence_after_seconds)
        self.assertEqual(self.build(fence_after_seconds=45.0, ledger=self.directory / "fence.db").fence_after_seconds,
                         45.0)

    def test_worker_service_takes_the_fence_delay_only_from_its_trusted_config(self):
        from contextlib import ExitStack
        from runtime import worker_service
        config = {"role": "executor", "context": "restricted-lab", "cluster_id": "sentinel-lab", "kubeconfig": "k",
                  "policy": "p", "baseline": "b", "allowed_targets": "t", "remote_dependencies": False,
                  "approvals": {"url": "https://127.0.0.1:1", "identity": "executor-1", "token_env": "TEST_TOKEN"}}
        for configured in ({}, {"fence_after_seconds": 45}):
            with self.subTest(configured=configured), \
                    patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}), patch("runtime.approval_http.ApprovalClient"), \
                    patch("runtime.live_executor.build_adapters") as build, ExitStack() as stack:
                worker_service.adapter(dict(config, **configured), self.directory, stack)
                self.assertEqual(build.call_args.kwargs["fence_after_seconds"], configured.get("fence_after_seconds"))

    def test_worker_service_gives_both_guarded_roles_the_cve_gate_scans(self):
        from contextlib import ExitStack
        from runtime import worker_service
        scans = self.directory / "scans"
        scans.mkdir()
        (scans / "a.json").write_text(json.dumps(gate_scan("demo/sentinel@sha256:" + "a" * 64)), encoding="utf-8")
        config = {"context": "restricted-lab", "cluster_id": "sentinel-lab", "kubeconfig": "k",
                  "policy": "p", "baseline": "b", "allowed_targets": "t", "remote_dependencies": False,
                  "image_scans": str(scans), "cve_exceptions": None,
                  "approvals": {"url": "https://127.0.0.1:1", "identity": "worker-1", "token_env": "TEST_TOKEN"}}
        with patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}), patch("runtime.approval_http.ApprovalClient"), \
                patch("runtime.live_executor.build_adapters") as build, ExitStack() as stack:
            worker_service.adapter(dict(config, role="executor"), self.directory, stack)
            self.assertEqual(build.call_args.kwargs["image_scans"], str(scans))
            guard = worker_service.adapter(dict(config, role="guard"), self.directory, stack)
        reader = guard.authorizer.scan_reader
        self.assertEqual([item["image"] for item in reader(["demo/sentinel@sha256:" + "a" * 64])["scans"]],
                         ["demo/sentinel@sha256:" + "a" * 64])
        with patch.dict(os.environ, {"TEST_TOKEN": "t" * 40}), patch("runtime.approval_http.ApprovalClient"), \
                ExitStack() as stack:
            config.pop("image_scans")
            self.assertIsNone(worker_service.adapter(dict(config, role="guard"), self.directory, stack).authorizer.scan_reader)

    def test_cli_exit_code_for_superseded_write(self):
        self.cluster.external_write()
        status, stdout, stderr = self.call_cli(self.arguments(reconciliation=True))
        self.assertEqual(status, 5, stderr)
        self.assertEqual(json.loads(stdout)["status"], "SUPERSEDED")
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

    # Per-target baselines (P3.09): DIR/<target name>.json, refused for another target.

    def baselines(self, **files):
        folder = self.directory / "baselines"
        folder.mkdir(exist_ok=True)
        for name, baseline in files.items():
            (folder / (name + ".json")).write_text(json.dumps(baseline), encoding="utf-8")
        return folder

    def test_baselines_directory_supplies_the_proposal_targets_baseline(self):
        folder = self.baselines(**{"demo-api": self.baseline})
        adapters = self.build(baseline=None, baselines=folder)
        self.assertEqual(execute(self.proposal, adapters)["status"], "APPLIED")
        self.assertEqual(len(self.cluster.writes), 1)

    def test_baselines_directory_refuses_a_baseline_for_another_target(self):
        other = deepcopy(self.baseline)
        other["target"] = dict(other["target"], name="cartservice", uid="cart-uid")
        folder = self.baselines(**{"demo-api": other})
        authorizer = self.authorizer(baseline=None, baselines=folder)
        with self.assertRaisesRegex(Exception, "another target"):
            authorizer.validate(self.proposal, self.snapshot, now=NOW)
        adapters = self.build(baseline=None, baselines=folder)
        result = execute(self.proposal, adapters)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_baselines_directory_without_the_targets_file_writes_nothing(self):
        folder = self.baselines(cartservice=self.baseline)
        result = execute(self.proposal, self.build(baseline=None, baselines=folder))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_baselines_directory_cannot_name_a_path_outside_it(self):
        folder = self.baselines(**{"demo-api": self.baseline})
        authorizer = self.authorizer(baseline=None, baselines=folder)
        for name in ("../demo-api", "Demo-API", ""):
            proposal = dict(self.proposal, target=dict(self.proposal["target"], name=name))
            with self.subTest(name=name), self.assertRaisesRegex(Exception, "no valid baseline name"):
                authorizer.validate(proposal, self.snapshot, now=NOW)

    def test_one_baseline_or_a_baselines_directory_but_not_both(self):
        folder = self.baselines(**{"demo-api": self.baseline})
        with self.assertRaisesRegex(Exception, "either one baseline or a baselines directory"):
            self.build(baselines=folder)
        with self.assertRaises(ValueError):
            self.build(baseline=None)
        for ledger, locks in ((folder / "executor.db", self.directory / "locks"),
                              (self.directory / "executor.db", folder / "locks"), (self.directory / "executor.db", folder)):
            with self.subTest(ledger=ledger.name, locks=locks.name), self.assertRaises(ValueError):
                self.build(baseline=None, baselines=folder, ledger=ledger, locks=locks)
        self.factory.assert_not_called()

    def test_cli_accepts_a_baselines_directory(self):
        folder = self.baselines(**{"demo-api": self.baseline})
        arguments = self.arguments()
        index = arguments.index("--baseline")
        arguments[index:index + 2] = ["--baselines", str(folder)]
        status, stdout, stderr = self.call_cli(arguments)
        self.assertEqual(status, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "APPLIED")

    def test_a_two_target_allowlist_still_refuses_a_third(self):
        cart = dict(self.proposal["target"], name="cartservice", uid="cart-uid")
        shipping = dict(self.proposal["target"], name="shippingservice", uid="shipping-uid")
        self.paths["allowed_targets"].write_text(json.dumps([cart, shipping]), encoding="utf-8")
        self.factory.side_effect = RealDeploymentAPI
        calls = []

        def forbidden_runner(*args, **kwargs):
            calls.append(args)
            raise AssertionError("A target outside the allowlist must never reach kubectl")

        adapters = self.build(runner=forbidden_runner)
        self.assertEqual(execute(self.proposal, adapters)["status"], "FAILED")
        self.assertEqual(calls, [])

    # Per-target Guard ceilings come from the catalog (experiments/boutique/catalog.json).

    def ceiling_decision(self, target, cpu):
        from experiments.boutique.harness.targets import profile
        from experiments.recovery.live import policy_ceilings
        self.policy.update(policy_ceilings(profile(target).ceilings))
        resources = self.baseline["pod_template"]["spec"]["containers"][0]["resources"]
        resources["requests"]["cpu"] = cpu
        resources["limits"]["cpu"] = "500m"  # Within every target's limit ceiling; only the request differs.
        self.bind_template()
        self.save_inputs()
        return self.authorizer().validate(self.proposal, self.snapshot, now=NOW).decision

    def test_cart_ceilings_allow_the_cart_baseline_request(self):
        # cartservice requests 200m CPU upstream; the shipping ceiling (100m) refuses it.
        self.assertEqual(self.ceiling_decision("cartservice", "200m")["verdict"], "ALLOW")
        shipping = self.ceiling_decision("shippingservice", "200m")
        self.assertEqual(shipping["verdict"], "REJECT")
        self.assertIn("RESOURCE_REQUEST_EXCEEDS_POLICY", shipping["reason_codes"])

    def test_cart_ceilings_still_reject_an_over_ceiling_template(self):
        decision = self.ceiling_decision("cartservice", "400m")
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertEqual(decision["reason_codes"],
                         ["RESOURCE_REQUEST_EXCEEDS_POLICY", "WORKLOAD_POD_REQUEST_EXCEEDS_POLICY"])

    def test_shipping_ceilings_are_the_values_the_live_policy_always_used(self):
        from experiments.boutique.harness.targets import profile
        from experiments.recovery.live import LAB_CEILINGS, policy_ceilings
        expected = {"resource_constraints": {"require_requests_and_limits": True,
                        "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
                        "max_container_limits": {"cpu": "500m", "memory": "128Mi"}},
                    "workload_constraints": {"max_pod_requests": {"cpu": "250m", "memory": "128Mi"},
                        "max_rollout_requests": {"cpu": "1", "memory": "512Mi"},
                        "max_replicas": 2, "max_surge_replicas": 1}}
        self.assertEqual(policy_ceilings(profile("shippingservice").ceilings), expected)
        self.assertEqual(policy_ceilings(LAB_CEILINGS), expected)


if __name__ == "__main__":
    unittest.main()
