"""Eligibility/security failures are distinguishable from unavailable evidence."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guard.common import GuardInputError
from services.guard.eligibility import assess, referenced_objects, validate_policy


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.policy = {
            "baseline_constraints": {
                "max_age_seconds": 300,
                "min_healthy_duration_seconds": 30,
                "trusted_sources": ["sentinel-verifier"],
                "require_health_evidence": True,
                "require_compatible_references": True,
                "require_image_digests": True,
            },
            "security_constraints": {
                "deny_privileged": True,
                "deny_host_access": True,
                "allowed_image_registries": ["docker.io"],
                "allowed_service_accounts": ["default"],
                "forbid_service_account_change": True,
            },
        }
        self.spec = self.baseline["pod_template"]["spec"]
        self.container = self.spec["containers"][0]
        self.container["image"] = "demo/sentinel@sha256:" + "a" * 64
        self.baseline["health_evidence"] = {
            "source": "sentinel-verifier",
            "evidence_id": "healthy-window-1",
            "status": "HEALTHY",
            "target": deepcopy(self.proposal["target"]),
            "template_sha256": self.baseline["template_sha256"],
            "window_start": "2026-10-10T11:58:30Z",
            "window_end": self.baseline["healthy_observed_at"],
        }

    def run_checks(self):
        validate_policy(self.policy)
        return assess(self.proposal, self.live, self.baseline, self.policy, NOW)

    def failures(self, status="FAIL"):
        return [result for result in self.run_checks() if result["status"] == status]

    def assert_finding(self, code, status="FAIL", path=None):
        results = [result for result in self.run_checks() if result["code"] == code and result["status"] == status]
        self.assertTrue(results, f"Missing {status} finding {code}")
        if path is not None:
            self.assertIn(path, [result["path"] for result in results])
        return results

    def add_reference(self):
        self.container["envFrom"] = [{"configMapRef": {"name": "settings"}}]
        record = {"kind": "ConfigMap", "namespace": self.proposal["target"]["namespace"], "name": "settings", "uid": "config-uid", "resource_version": "4"}
        self.baseline["reference_versions"] = [record]
        self.live["reference_versions"] = [deepcopy(record)]

    def test_safe_baseline_passes_all_enabled_checks(self):
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.failures("UNKNOWN"), [])
        self.assert_finding("BASELINE_HEALTH_DURATION", "PASS")

    def test_absent_policy_blocks_are_backwards_compatible(self):
        self.policy = {}
        self.assertEqual([result["status"] for result in self.run_checks()], ["SKIP"])

    def test_nonrollback_actions_skip_checks(self):
        self.proposal["action"] = "SCALE"
        self.assertEqual([result["status"] for result in self.run_checks()], ["SKIP"])

    def test_malformed_policies_reject_instead_of_silently_ignoring_rules(self):
        for block, key, value in (
            ("baseline_constraints", "max_age_seconds", True),
            ("baseline_constraints", "min_healthy_duration_seconds", -1),
            ("baseline_constraints", "trusted_sources", []),
            ("baseline_constraints", "require_image_digests", "yes"),
            ("baseline_constraints", "max_age_second", 10),
            ("security_constraints", "deny_host_access", 1),
            ("security_constraints", "allowed_image_registries", ["docker.io.evil/path"]),
            ("security_constraints", "allowed_image_registries", ["https://docker.io"]),
            ("security_constraints", "allowed_service_accounts", [""]),
        ):
            with self.subTest(key=key, value=value):
                policy = deepcopy(self.policy)
                policy[block][key] = value
                with self.assertRaises(GuardInputError):
                    validate_policy(policy)

    def test_missing_baseline_cannot_be_certified(self):
        self.baseline = None
        self.assert_finding("BASELINE_ELIGIBILITY_MISSING", "UNKNOWN")

    def test_missing_health_evidence_escalates(self):
        del self.baseline["health_evidence"]
        self.assert_finding("BASELINE_HEALTH_EVIDENCE_MISSING", "UNKNOWN")

    def test_health_evidence_source_status_target_and_template_are_bound(self):
        for key, value, code in (
            ("source", "untrusted-model", "BASELINE_HEALTH_SOURCE"),
            ("status", "FAILED", "BASELINE_HEALTH_STATUS"),
            ("target", dict(self.proposal["target"], uid="other-uid"), "BASELINE_HEALTH_TARGET"),
            ("template_sha256", "b" * 64, "BASELINE_HEALTH_TEMPLATE"),
        ):
            with self.subTest(key=key):
                original = deepcopy(self.baseline["health_evidence"])
                self.baseline["health_evidence"][key] = value
                self.assert_finding(code)
                self.baseline["health_evidence"] = original

    def test_malformed_evidence_is_a_failure(self):
        self.baseline["health_evidence"]["window_start"] = "not-a-time"
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID")

    def test_old_baseline_rejects(self):
        self.policy["baseline_constraints"]["max_age_seconds"] = 20
        self.assert_finding("BASELINE_AGE")

    def test_future_reversed_inconsistent_and_short_health_windows_reject(self):
        for start, end, code in (
            ("2026-10-10T12:01:00Z", "2026-10-10T12:02:00Z", "BASELINE_HEALTH_WINDOW"),
            ("2026-10-10T12:00:00Z", "2026-10-10T11:59:30Z", "BASELINE_HEALTH_WINDOW"),
            ("2026-10-10T11:58:30Z", "2026-10-10T11:59:20Z", "BASELINE_HEALTH_WINDOW"),
            ("2026-10-10T11:59:20Z", "2026-10-10T11:59:30Z", "BASELINE_HEALTH_DURATION"),
        ):
            with self.subTest(start=start, end=end):
                self.baseline["health_evidence"]["window_start"] = start
                self.baseline["health_evidence"]["window_end"] = end
                self.assert_finding(code)

    def test_mutable_malformed_and_uppercase_digests_reject(self):
        for image in ("demo/sentinel:latest", "demo/sentinel@sha256:" + "a" * 63, "demo/sentinel@sha256:" + "A" * 64):
            with self.subTest(image=image):
                self.container["image"] = image
                self.assert_finding("BASELINE_IMAGE_DIGEST")

    def test_every_regular_and_init_container_is_checked(self):
        self.spec["initContainers"] = [{"name": "unsafe-init", "image": "evil.example/init:latest", "securityContext": {"privileged": True}}]
        self.assert_finding("BASELINE_IMAGE_DIGEST", path="baseline.pod_template.spec.initContainers[0].image")
        self.assert_finding("SECURITY_IMAGE_REGISTRY")
        self.assert_finding("SECURITY_PRIVILEGED")
        self.spec["containers"].append({"name": "unsafe-sidecar", "image": "evil.example/sidecar:latest", "securityContext": {"allowPrivilegeEscalation": True}})
        self.assert_finding("SECURITY_PRIVILEGE_ESCALATION", path="baseline.pod_template.spec.containers[1].securityContext.allowPrivilegeEscalation")

    def test_same_reference_identity_and_version_pass(self):
        self.add_reference()
        self.assert_finding("BASELINE_REFERENCE_CHANGED", "PASS")
        self.assertEqual(self.failures(), [])

    def test_reference_recreation_or_modification_rejects(self):
        for key in ("uid", "resource_version"):
            with self.subTest(key=key):
                self.add_reference()
                self.live["reference_versions"][0][key] = "changed"
                self.assert_finding("BASELINE_REFERENCE_CHANGED")

    def test_missing_saved_or_current_reference_evidence_escalates(self):
        for location in ("saved", "current"):
            with self.subTest(location=location):
                self.add_reference()
                (self.baseline if location == "saved" else self.live).pop("reference_versions")
                self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN")

    def test_secret_values_cannot_appear_in_reference_metadata_or_report(self):
        self.add_reference()
        self.live["reference_versions"][0]["data"] = {"password": "NEVER-PRINT-ME"}
        result = self.assert_finding("BASELINE_ELIGIBILITY_INVALID")
        self.assertNotIn("NEVER-PRINT-ME", json.dumps(result))

    def test_env_volume_projected_and_pull_secret_references_are_discovered(self):
        self.container["env"] = [{"name": "DB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "db", "key": "password"}}}]
        self.spec["volumes"] = [
            {"name": "config", "configMap": {"name": "settings"}},
            {"name": "secret", "secret": {"secretName": "volume-secret"}},
            {"name": "projected", "projected": {"sources": [{"secret": {"name": "projected-secret"}}, {"configMap": {"name": "projected-config"}}]}},
        ]
        self.spec["imagePullSecrets"] = [{"name": "registry-auth"}]
        findings = self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN")
        self.assertEqual(len(findings), 6)

    def test_optional_references_still_need_compatibility_evidence(self):
        self.container["envFrom"] = [{"secretRef": {"name": "optional", "optional": True}}]
        self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN")

    def test_external_storage_and_new_projected_forms_are_explicitly_unknown(self):
        for source in ({"csi": {"driver": "example.driver"}}, {"persistentVolumeClaim": {"claimName": "data"}}, {"projected": {"sources": [{"clusterTrustBundle": {"name": "trust"}}]}}):
            with self.subTest(source=source):
                self.spec["volumes"] = [dict(name="external", **source)]
                self.assert_finding("BASELINE_REFERENCE_UNSUPPORTED", "UNKNOWN")

    def test_malformed_reference_list_and_duplicate_identities_reject(self):
        self.add_reference()
        self.baseline["reference_versions"].append(deepcopy(self.baseline["reference_versions"][0]))
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID")
        self.baseline["reference_versions"] = "bad"
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID")

    def test_pod_host_namespace_host_path_and_host_port_reject(self):
        self.spec.update(hostNetwork=True, hostPID=True, hostIPC=True, volumes=[{"name": "host", "hostPath": {"path": "/"}}])
        self.container["ports"] = [{"containerPort": 8080, "hostPort": 8080}]
        findings = self.assert_finding("SECURITY_HOST_ACCESS")
        self.assertEqual(len(findings), 5)

    def test_privilege_escalation_capabilities_root_and_seccomp_reject(self):
        self.container["securityContext"].update(privileged=True, allowPrivilegeEscalation=True, runAsUser=0, capabilities={"add": ["SYS_ADMIN"]}, seccompProfile={"type": "Unconfined"})
        for code in ("SECURITY_PRIVILEGED", "SECURITY_PRIVILEGE_ESCALATION", "SECURITY_CAPABILITIES", "SECURITY_ROOT_USER", "SECURITY_SECCOMP"):
            self.assert_finding(code)

    def test_implicit_privilege_escalation_requires_explicit_disable(self):
        del self.container["securityContext"]["allowPrivilegeEscalation"]
        self.assert_finding("SECURITY_PRIVILEGE_ESCALATION")

    def test_registry_allowlist_uses_exact_host_including_ports(self):
        for image in ("docker.io.evil.example/x@sha256:" + "a" * 64, "docker.io:5000/x@sha256:" + "a" * 64, "user@docker.io/x@sha256:" + "a" * 64):
            with self.subTest(image=image):
                self.container["image"] = image
                self.assertTrue(any(result["status"] == "FAIL" for result in self.run_checks()))
        self.container["image"] = "docker.io/team/demo@sha256:" + "a" * 64
        self.assert_finding("SECURITY_IMAGE_REGISTRY", "PASS")

    def test_service_account_allowlist_and_change_are_separate_rules(self):
        self.spec["serviceAccountName"] = "admin"
        self.assert_finding("SECURITY_SERVICE_ACCOUNT")
        self.assert_finding("SECURITY_SERVICE_ACCOUNT_CHANGE")
        self.policy["security_constraints"]["allowed_service_accounts"].append("admin")
        self.assert_finding("SECURITY_SERVICE_ACCOUNT", "PASS")
        self.assert_finding("SECURITY_SERVICE_ACCOUNT_CHANGE")

    def test_unavailable_current_account_escalates(self):
        del self.live["deployment"]["spec"]["template"]
        self.assert_finding("SECURITY_SERVICE_ACCOUNT_UNKNOWN", "UNKNOWN")

    def test_security_type_errors_fail_closed(self):
        self.spec["hostNetwork"] = "false"
        self.assert_finding("SECURITY_TEMPLATE_INVALID")

    def test_invalid_or_ephemeral_container_templates_fail_closed(self):
        self.spec["ephemeralContainers"] = []
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID")
        del self.spec["ephemeralContainers"]
        self.spec["containers"] = []
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID")

    def test_adapter_reference_enumeration_returns_only_metadata(self):
        self.add_reference()
        self.container["env"].append({"name": "PASSWORD", "value": "NEVER-PRINT-ME"})
        refs, findings = referenced_objects(self.baseline["pod_template"], self.proposal["target"]["namespace"])
        self.assertEqual(refs, [{"kind": "ConfigMap", "namespace": self.proposal["target"]["namespace"], "name": "settings"}])
        self.assertEqual(findings, [])
        self.assertNotIn("NEVER-PRINT-ME", json.dumps((refs, findings)))

    def test_adapter_reference_enumeration_preserves_unsupported_and_malformed(self):
        self.spec["volumes"] = [{"name": "external", "csi": {"driver": "example"}}]
        _, findings = referenced_objects(self.baseline["pod_template"], "test")
        self.assertEqual(findings[0]["status"], "UNKNOWN")
        self.spec["volumes"] = "malformed"
        _, findings = referenced_objects(self.baseline["pod_template"], "test")
        self.assertEqual(findings[0]["status"], "FAIL")


if __name__ == "__main__":
    unittest.main()
