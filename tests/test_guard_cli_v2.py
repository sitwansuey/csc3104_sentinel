"""Bound decisions, atomic audit reports and guarded live preview handoffs."""

import copy
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.guard.core import proposal_fingerprint, template_sha256
from services.guard.cli import main
from shared.contracts import validate_message


def fixture(relative):
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z")


class GuardCliV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        self.approval = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.policy["policy_version"],
            "operator_id": "fixture-operator", "decision": "APPROVED",
            "approved_at": "2026-10-10T12:00:35Z", "expires_at": "2026-10-10T12:01:35Z",
        }
        self.write_inputs()

    def path(self, name):
        return self.directory / f"{name}.json"

    def write_inputs(self):
        for name, value in (("proposal", self.proposal), ("policy", self.policy),
                            ("baseline", self.baseline), ("dependencies", self.dependencies),
                            ("live-state", self.live), ("approval", self.approval)):
            self.path(name).write_text(json.dumps(value), encoding="utf-8")

    def args(self, *, live=False, approval=True):
        names = ["proposal", "policy", "baseline", "dependencies"]
        if approval:
            names.append("approval")
        result = [arg for name in names for arg in (f"--{name}", str(self.path(name)))]
        if live:
            result += ["--context", "team-context", "--cluster-id", "sentinel-lab"]
        else:
            result += ["--live-state", str(self.path("live-state")), "--now", "2026-10-10T12:00:40Z"]
        return result

    def run_cli(self, args):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            with patch("sys.stderr", new_callable=io.StringIO) as stderr:
                code = main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def configure_live(self):
        now = datetime.now(timezone.utc)
        self.policy.update({
            "profile": "live", "decision_ttl_seconds": 30,
            "max_approval_lifetime_seconds": 120, "max_state_age_seconds": 120,
            "max_dependency_age_seconds": 120,
            "baseline_constraints": {
                "max_age_seconds": 300, "min_healthy_duration_seconds": 30,
                "trusted_sources": ["sentinel-verifier"], "require_health_evidence": True,
                "require_compatible_references": True, "require_image_digests": True,
            },
            "security_constraints": {
                "deny_privileged": True, "deny_host_access": True,
                "allowed_image_registries": ["docker.io"],
                "allowed_service_accounts": ["default"], "forbid_service_account_change": True,
            },
            "resource_constraints": {
                "require_requests_and_limits": True,
                "max_container_requests": {"cpu": "1", "memory": "256Mi"},
                "max_container_limits": {"cpu": "1", "memory": "256Mi"},
            },
            "workload_constraints": {
                "max_pod_requests": {"cpu": "1", "memory": "256Mi"},
                "max_rollout_requests": {"cpu": "2", "memory": "512Mi"},
                "max_replicas": 2, "max_surge_replicas": 1,
            },
        })
        self.live["captured_at"] = timestamp(now)
        self.dependencies["checked_at"] = timestamp(now - timedelta(seconds=1))
        self.baseline["healthy_observed_at"] = timestamp(now - timedelta(seconds=30))
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = "demo/sentinel@sha256:" + "a" * 64
        self.bind_template()
        self.baseline["reference_versions"] = []
        self.baseline["health_evidence"] = {
            "source": "sentinel-verifier", "evidence_id": "health-1", "status": "HEALTHY",
            "target": copy.deepcopy(self.proposal["target"]),
            "template_sha256": self.baseline["template_sha256"],
            "window_start": timestamp(now - timedelta(seconds=60)),
            "window_end": self.baseline["healthy_observed_at"],
        }
        self.approval.update(approved_at=timestamp(now - timedelta(seconds=1)),
                             expires_at=timestamp(now + timedelta(seconds=60)))
        self.write_inputs()

    def bind_template(self):
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        if "health_evidence" in self.baseline:
            self.baseline["health_evidence"]["template_sha256"] = digest
        self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)

    def preview(self, status="PASSED"):
        return {
            "status": status, "proposal_sha256": proposal_fingerprint(self.proposal),
            "target": copy.deepcopy(self.proposal["target"]), "resource_version": "102",
            "template_sha256": self.baseline["template_sha256"],
            "checked_at": timestamp(datetime.now(timezone.utc)), "details_code": "FIXTURE",
            "admitted_template_sha256": self.baseline["template_sha256"] if status == "PASSED" else None,
        }

    def test_report_is_complete_atomic_private_and_decision_is_version_two(self):
        report = self.path("report")
        report.write_text("old report", encoding="utf-8")
        code, stdout, stderr = self.run_cli([*self.args(), "--report", str(report)])
        self.assertEqual((code, stderr), (0, ""))
        decision = json.loads(stdout)
        validate_message(decision)
        self.assertEqual(decision["schema_version"], "2.0")
        explanation = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(explanation["kind"], "guard_report")
        self.assertEqual(explanation["proposal_sha256"], decision["proposal_sha256"])
        self.assertTrue(explanation["checks"])
        self.assertEqual(list(self.directory.glob(".sentinel-report-*")), [])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)

    def test_report_failure_emits_no_authorization_and_preserves_existing_report(self):
        report = self.path("report")
        report.write_text("preserve me", encoding="utf-8")
        with patch("services.guard.cli.os.replace", side_effect=OSError("secret-details")):
            code, stdout, stderr = self.run_cli([*self.args(), "--report", str(report)])
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "REPORT_UNAVAILABLE")
        self.assertNotIn("secret-details", stderr)
        self.assertEqual(report.read_text(), "preserve me")
        self.assertEqual(list(self.directory.glob(".sentinel-report-*")), [])

    def test_report_cannot_overwrite_inputs_or_kubeconfig(self):
        for name in ("proposal", "policy", "baseline", "dependencies", "approval", "live-state"):
            with self.subTest(name=name):
                before = self.path(name).read_bytes()
                code, stdout, stderr = self.run_cli([*self.args(), "--report", str(self.path(name))])
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
                self.assertEqual(self.path(name).read_bytes(), before)
        kubeconfig = self.path("kubeconfig")
        kubeconfig.write_text("private credentials", encoding="utf-8")
        with patch("services.guard.cli.read_deployment") as read:
            code, stdout, stderr = self.run_cli([*self.args(live=True), "--kubeconfig", str(kubeconfig), "--report", str(kubeconfig)])
        self.assertEqual((code, stdout), (1, ""))
        read.assert_not_called()
        self.assertEqual(kubeconfig.read_text(), "private credentials")

    def test_report_cannot_overwrite_an_input_via_hardlink(self):
        alias = self.path("alias")
        os.link(self.path("proposal"), alias)
        code, stdout, stderr = self.run_cli([*self.args(), "--report", str(alias)])
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")

    def test_offline_preview_fixture_is_bound_and_cannot_be_report_destination(self):
        preview = self.preview()
        preview["checked_at"] = "2026-10-10T12:00:39Z"
        self.path("preview").write_text(json.dumps(preview), encoding="utf-8")
        args = [*self.args(), "--preview", str(self.path("preview"))]
        code, stdout, stderr = self.run_cli(args)
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["verdict"], "ALLOW")
        code, stdout, stderr = self.run_cli([*args, "--report", str(self.path("preview"))])
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")

    def test_live_development_or_incomplete_policy_fails_before_any_read(self):
        for live_profile in (False, True):
            with self.subTest(live_profile=live_profile):
                if live_profile:
                    self.configure_live()
                    del self.policy["max_state_age_seconds"]
                    self.write_inputs()
                with patch("services.guard.cli.read_deployment") as read:
                    with patch("services.guard.cli.read_reference_versions") as refs:
                        with patch("services.guard.cli.server_dry_run") as preview:
                            code, stdout, stderr = self.run_cli(self.args(live=True))
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "INVALID_GUARD_INPUT")
                read.assert_not_called()
                refs.assert_not_called()
                preview.assert_not_called()

    def test_preview_modes_are_explicit_and_fixture_preview_is_forbidden_live(self):
        for args in ([*self.args(), "--server-dry-run"],
                     [*self.args(live=True), "--preview", str(self.path("preview"))],
                     [*self.args(), "--server-dry-run", "--preview", "anything"]):
            with self.subTest(args=args):
                with patch("services.guard.cli.read_deployment") as read:
                    with patch("services.guard.cli.server_dry_run") as preview:
                        code, stdout, stderr = self.run_cli(args)
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")
                read.assert_not_called()
                preview.assert_not_called()

    def test_required_preview_runs_only_after_remaining_checks_pass(self):
        self.configure_live()
        self.policy.update(require_server_dry_run=True, max_server_dry_run_age_seconds=10)
        self.write_inputs()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            with patch("services.guard.cli.server_dry_run", return_value=self.preview()) as preview:
                code, stdout, stderr = self.run_cli([*self.args(live=True), "--server-dry-run"])
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["verdict"], "ALLOW")
        preview.assert_called_once()

    def test_optional_preview_failure_and_mutation_never_allow(self):
        self.configure_live()
        for status, code_expected in (("UNKNOWN", 3), ("REJECTED", 2)):
            with self.subTest(status=status):
                with patch("services.guard.cli.read_deployment", return_value=self.live):
                    with patch("services.guard.cli.server_dry_run", return_value=self.preview(status)) as preview:
                        code, stdout, stderr = self.run_cli([*self.args(live=True), "--server-dry-run"])
                self.assertEqual((code, stderr), (code_expected, ""))
                self.assertNotEqual(json.loads(stdout)["verdict"], "ALLOW")
                preview.assert_called_once()

    def test_hard_rejection_or_missing_approval_skip_admission_request(self):
        self.configure_live()
        self.policy.update(require_server_dry_run=True, max_server_dry_run_age_seconds=10)
        self.write_inputs()
        for approval, change_version, expected in ((False, False, 3), (True, True, 2)):
            with self.subTest(approval=approval):
                live = copy.deepcopy(self.live)
                if change_version:
                    live["deployment"]["metadata"]["resourceVersion"] = "103"
                with patch("services.guard.cli.read_deployment", return_value=live):
                    with patch("services.guard.cli.server_dry_run") as preview:
                        code, stdout, stderr = self.run_cli([*self.args(live=True, approval=approval), "--server-dry-run"])
                self.assertEqual((code, stderr), (expected, ""))
                self.assertNotEqual(json.loads(stdout)["verdict"], "ALLOW")
                preview.assert_not_called()

    def test_approval_expiring_during_admission_is_checked_with_a_fresh_clock(self):
        self.configure_live()
        initial = datetime.now(timezone.utc)
        later = initial + timedelta(seconds=65)
        admitted = self.preview()
        admitted["checked_at"] = timestamp(later)
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            with patch("services.guard.cli.server_dry_run", return_value=admitted) as preview:
                with patch("services.guard.decisions._clock", side_effect=[initial, later]):
                    code, stdout, stderr = self.run_cli([*self.args(live=True), "--server-dry-run"])
        self.assertEqual((code, stderr), (3, ""))
        self.assertIn("APPROVAL_EXPIRED", json.loads(stdout)["reason_codes"])
        preview.assert_called_once()

    def test_unsupported_reference_forms_remain_unknown_and_skip_admission(self):
        self.configure_live()
        self.baseline["pod_template"]["spec"]["volumes"] = [
            {"name": "external-data", "csi": {"driver": "example.invalid"}}]
        self.bind_template()
        self.write_inputs()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            with patch("services.guard.cli.read_reference_versions") as refs:
                with patch("services.guard.cli.server_dry_run") as preview:
                    code, stdout, stderr = self.run_cli([*self.args(live=True), "--server-dry-run"])
        self.assertEqual((code, stderr), (3, ""))
        self.assertIn("BASELINE_REFERENCE_UNSUPPORTED", json.loads(stdout)["reason_codes"])
        refs.assert_not_called()
        preview.assert_not_called()

    def test_live_references_are_collected_as_metadata_and_checked(self):
        self.configure_live()
        self.baseline["pod_template"]["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "settings"}}]
        reference = {"kind": "ConfigMap", "namespace": "sentinel-demo", "name": "settings",
                     "uid": "config-uid", "resource_version": "4"}
        self.baseline["reference_versions"] = [reference]
        self.bind_template()
        self.write_inputs()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            with patch("services.guard.cli.read_reference_versions", return_value=[reference]) as refs:
                code, stdout, stderr = self.run_cli(self.args(live=True))
        self.assertEqual((code, stderr), (0, ""))
        self.assertEqual(json.loads(stdout)["verdict"], "ALLOW")
        refs.assert_called_once_with(self.proposal["target"], [
            {"kind": "ConfigMap", "namespace": "sentinel-demo", "name": "settings"}],
            context="team-context", cluster_id="sentinel-lab", kubeconfig=None)

    def test_mismatched_live_identity_skips_reference_and_preview_calls(self):
        self.configure_live()
        self.baseline["pod_template"]["spec"]["containers"][0]["envFrom"] = [{"configMapRef": {"name": "settings"}}]
        self.bind_template()
        self.live["deployment"]["metadata"]["uid"] = "replacement"
        self.write_inputs()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            with patch("services.guard.cli.read_reference_versions") as refs:
                with patch("services.guard.cli.server_dry_run") as preview:
                    code, stdout, stderr = self.run_cli([*self.args(live=True), "--server-dry-run"])
        self.assertEqual((code, stderr), (2, ""))
        self.assertEqual(json.loads(stdout)["verdict"], "REJECT")
        refs.assert_not_called()
        preview.assert_not_called()

    def configure_cve_gate(self):
        """P5.02: a live policy with the CVE gate, a config-only rollback, and a scan directory."""
        self.configure_live()
        image = self.baseline["pod_template"]["spec"]["containers"][0]["image"]
        self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = image
        scanner = "aquasec/trivy@sha256:" + "f" * 64
        self.policy["security_constraints"]["image_vulnerability_gate"] = {
            "block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression", "max_scan_age_seconds": 3600,
            "trusted_scanner_images": [scanner]}
        self.write_inputs()
        scans = self.directory / "scans"
        scans.mkdir()

        def write_scan(name, target, *findings):
            (scans / name).write_text(json.dumps({
                "image": target, "status": "FINDINGS" if findings else "NO_KNOWN_CVES", "scanner_image": scanner,
                "scanned_at": timestamp(datetime.now(timezone.utc) - timedelta(seconds=60)),
                "findings": [{"id": cve, "severity": level} for cve, level in findings]}), encoding="utf-8")
        write_scan("current.json", image, ("CVE-1", "HIGH"))
        return scans, write_scan

    def test_live_cve_gate_reads_scans_records_inherited_and_rejects_a_worse_baseline(self):
        scans, write_scan = self.configure_cve_gate()
        report = self.directory / "report.json"
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            code, stdout, stderr = self.run_cli([*self.args(live=True), "--image-scans", str(scans),
                                                 "--report", str(report)])
        self.assertEqual((code, stderr), (0, ""))
        self.assertIn(("IMAGE_CVE_INHERITED", "PASS"),
                      {(item["code"], item["status"]) for item in json.loads(report.read_text())["checks"]})
        worse = "demo/sentinel@sha256:" + "b" * 64
        write_scan("worse.json", worse, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL"))
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = worse
        self.bind_template()
        self.write_inputs()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            code, stdout, stderr = self.run_cli([*self.args(live=True), "--image-scans", str(scans)])
        self.assertEqual((code, json.loads(stdout)["reason_codes"]), (2, ["IMAGE_CVE_REGRESSION"]))

    def test_live_cve_gate_without_scans_escalates_and_unreadable_scans_authorize_nothing(self):
        scans, _ = self.configure_cve_gate()
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            code, stdout, _ = self.run_cli(self.args(live=True))
        self.assertEqual((code, json.loads(stdout)["reason_codes"]), (3, ["IMAGE_SCANS_MISSING"]))
        bad = self.directory / "bad-exceptions.json"
        bad.write_text("[]", encoding="utf-8")
        with patch("services.guard.cli.read_deployment", return_value=self.live):
            code, stdout, stderr = self.run_cli([*self.args(live=True), "--image-scans", str(scans),
                                                 "--cve-exceptions", str(bad)])
        self.assertEqual((code, stdout), (1, ""))
        self.assertEqual(json.loads(stderr)["error"]["code"], "IMAGE_SCANS_UNAVAILABLE")

    def test_scan_options_are_for_live_reads_only(self):
        for extra in (["--image-scans", "scans"], ["--context", "c", "--cluster-id", "x", "--cve-exceptions", "e"]):
            with self.subTest(extra=extra):
                arguments = self.args() if "--context" not in extra else self.args()[:-4]
                code, stdout, stderr = self.run_cli([*arguments, *extra])
                self.assertEqual((code, stdout), (1, ""))
                self.assertEqual(json.loads(stderr)["error"]["code"], "ARGUMENT_ERROR")


if __name__ == "__main__":
    unittest.main()
