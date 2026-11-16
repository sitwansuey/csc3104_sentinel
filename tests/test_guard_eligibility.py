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
from services.guard.core import evaluate, proposal_fingerprint, template_sha256
from services.guard.eligibility import assess, referenced_objects, validate_policy
from shared.contracts import validate_message


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

    def test_actions_that_start_no_pods_skip_checks(self):
        for action in ("NO_ACTION", "ESCALATE"):
            with self.subTest(action=action):
                self.proposal["action"] = action
                self.assertEqual([result["status"] for result in self.run_checks()], ["SKIP"])

    def test_scale_runs_the_same_checks_as_a_rollback(self):
        # P5.07: a SCALE starts new Pods from the live template, whose configuration is the baseline's.
        # The P5.02 CVE gate is the one exception: it covers ROLLBACK and RESTART only, so under
        # ImageGateEligibilityTests its findings are left out of the SCALE list.
        rollback = [(item["code"], item["status"]) for item in self.run_checks()]
        self.proposal["action"] = "SCALE"
        self.assertEqual([(item["code"], item["status"]) for item in self.run_checks()],
                         [(code, status) for code, status in rollback if not code.startswith("IMAGE_")])
        self.assertNotIn("SKIP", {status for _, status in rollback})

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


ENVOY = "envoyproxy/envoy@sha256:" + "e" * 64


def ingress_sidecar(image=ENVOY):
    """The shape of the protected template's TLS ingress sidecar (scripts/security/boutique_security_manifest.py)."""
    return {"name": "shipping-ingress", "image": image, "imagePullPolicy": "Never", "command": ["envoy"],
            "args": ["-c", "/config/envoy.json"], "ports": [{"containerPort": 8444}],
            "securityContext": {"runAsUser": 10001, "runAsNonRoot": True, "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            "volumeMounts": [{"name": "security-upstream", "mountPath": "/config", "readOnly": True},
                             {"name": "upstream", "mountPath": "/pki/upstream", "readOnly": True}]}


class ProtectedSidecarEligibilityTests(EligibilityTests):
    """P5.05: every eligibility rule still holds, and only the target's reviewed sidecar may run beside it."""

    def setUp(self):
        super().setUp()
        namespace = self.proposal["target"]["namespace"]
        self.spec["containers"].append(ingress_sidecar())
        self.spec["volumes"] = [{"name": "security-upstream", "configMap": {"name": "security-upstream"}},
                                {"name": "upstream", "secret": {"secretName": "security-upstream", "defaultMode": 288}}]
        self.policy["security_constraints"]["allowed_sidecars"] = [{
            "target": self.proposal["target"]["name"], "name": "shipping-ingress", "image": ENVOY,
            "mounts": [{"volume": "security-upstream", "mount_path": "/config",
                        "source": {"kind": "ConfigMap", "name": "security-upstream"}},
                       {"volume": "upstream", "mount_path": "/pki/upstream",
                        "source": {"kind": "Secret", "name": "security-upstream"}}]}]
        records = [{"kind": kind, "namespace": namespace, "name": "security-upstream", "uid": kind + "-uid",
                    "resource_version": "11"} for kind in ("ConfigMap", "Secret")]
        self.baseline["reference_versions"] = deepcopy(records)
        self.live["reference_versions"] = deepcopy(records)
        self.sidecar = self.spec["containers"][1]

    def test_safe_baseline_passes_all_enabled_checks(self):
        super().test_safe_baseline_passes_all_enabled_checks()
        for code in ("SECURITY_SIDECAR_IMAGE", "SECURITY_SIDECAR_MOUNTS", "SECURITY_SIDECAR_ENV",
                     "SECURITY_SIDECAR_NOT_ALLOWED", "SECURITY_SIDECAR_COMMAND", "SECURITY_SIDECAR_FIELDS"):
            self.assert_finding(code, "PASS")

    def test_a_lifecycle_hook_or_exec_probe_cannot_run_a_command_in_the_sidecar(self):
        # A hook or exec probe runs any shell beside the mounted key, whatever the pinned command and args.
        shell = {"exec": {"command": ["/bin/sh", "-c", "cat /pki/upstream/tls.key"]}}
        for key, value, field in (
            ("lifecycle", {"postStart": deepcopy(shell)}, "lifecycle"),
            ("lifecycle", {"preStop": deepcopy(shell)}, "lifecycle"),
            ("lifecycle", {"preStop": {"httpGet": {"port": 8444}}}, "lifecycle"),
            ("livenessProbe", dict(deepcopy(shell), periodSeconds=10), "livenessProbe.exec"),
            ("readinessProbe", deepcopy(shell), "readinessProbe.exec"),
            ("startupProbe", deepcopy(shell), "startupProbe.exec"),
        ):
            with self.subTest(key=key, value=value):
                self.setUp()
                self.sidecar[key] = value
                [finding] = self.assert_finding("SECURITY_SIDECAR_COMMAND", path="baseline.pod_template.spec.containers[1]")
                self.assertEqual(finding["actual"], ["baseline.pod_template.spec.containers[1]." + field])

    def test_unreviewed_sidecar_fields_are_rejected(self):
        for change in (
            lambda: self.sidecar.update(workingDir="/pki"),
            lambda: self.sidecar.update(stdin=True, tty=True),
            lambda: self.sidecar.update(volumeDevices=[{"name": "upstream", "devicePath": "/dev/xvda"}]),
            lambda: self.sidecar.update(restartPolicy="Always"),
            lambda: self.sidecar.update(readinessProbe={"tcpSocket": {"port": 8444, "host": "169.254.169.254"}}),
            lambda: self.sidecar.update(readinessProbe={"periodSeconds": 3}),
            lambda: self.sidecar.update(readinessProbe={"tcpSocket": {"port": 8444}, "httpGet": {"port": 8444}}),
            lambda: self.sidecar.update(livenessProbe={"grpc": {"port": 8444}, "extra": True}),
        ):
            with self.subTest(change=change):
                self.setUp()
                change()
                self.assert_finding("SECURITY_SIDECAR_FIELDS", path="baseline.pod_template.spec.containers[1]")
        self.setUp()
        self.sidecar["readinessProbe"] = "tcp"
        self.assert_finding("SECURITY_TEMPLATE_INVALID")

    def test_a_network_probe_and_kubernetes_defaults_are_reviewed_fields(self):
        # The live template is read back from the API server with its defaults filled in.
        self.sidecar.update(terminationMessagePath="/dev/termination-log", terminationMessagePolicy="File",
                            resources={"limits": {"cpu": "200m", "memory": "64Mi"}},
                            readinessProbe={"tcpSocket": {"port": 8444}, "periodSeconds": 3, "timeoutSeconds": 1,
                                            "successThreshold": 1, "failureThreshold": 3},
                            livenessProbe={"grpc": {"port": 8444}}, startupProbe={"httpGet": {"path": "/", "port": 8444}})
        self.assertEqual(self.failures(), [])
        self.assert_finding("SECURITY_SIDECAR_FIELDS", "PASS")

    def test_no_other_container_may_read_the_sidecars_secret(self):
        for change in (
            lambda: self.container.setdefault("volumeMounts", []).append(
                {"name": "upstream", "mountPath": "/stolen", "readOnly": True}),
            lambda: self.container["env"].append(
                {"name": "KEY", "valueFrom": {"secretKeyRef": {"name": "security-upstream", "key": "tls.key"}}}),
            lambda: self.container.update(envFrom=[{"secretRef": {"name": "security-upstream"}}]),
            lambda: (self.spec["volumes"].append({"name": "bundle", "projected": {"sources": [
                {"secret": {"name": "security-upstream"}}]}}),
                self.container.setdefault("volumeMounts", []).append({"name": "bundle", "mountPath": "/b"})),
        ):
            with self.subTest(change=change):
                self.setUp()
                change()
                [finding] = self.assert_finding("SECURITY_SIDECAR_MOUNTS", path="baseline.pod_template.spec.containers[0]")
                self.assertEqual(finding["actual"], ["security-upstream"])
        # The application container's own other references stay its own business.
        self.setUp()
        self.add_reference()
        self.assertEqual([item for item in self.failures() if item["code"] == "SECURITY_SIDECAR_MOUNTS"], [])

    def test_adapter_reference_enumeration_returns_only_metadata(self):
        # The reviewed sidecar's own ConfigMap and Secret are references like any other.
        self.add_reference()
        refs, findings = referenced_objects(self.baseline["pod_template"], self.proposal["target"]["namespace"])
        self.assertEqual([(ref["kind"], ref["name"]) for ref in refs],
                         [("ConfigMap", "security-upstream"), ("ConfigMap", "settings"), ("Secret", "security-upstream")])
        self.assertEqual(findings, [])

    def test_every_regular_and_init_container_is_checked(self):
        # The base test's unsafe sidecar lands after the reviewed one here, at containers[2].
        self.spec["containers"].append({"name": "unsafe-sidecar", "image": "evil.example/sidecar:latest",
                                        "securityContext": {"allowPrivilegeEscalation": True}})
        self.assert_finding("SECURITY_PRIVILEGE_ESCALATION",
                            path="baseline.pod_template.spec.containers[2].securityContext.allowPrivilegeEscalation")
        self.assert_finding("SECURITY_IMAGE_REGISTRY", path="baseline.pod_template.spec.containers[2].image")
        self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED")

    def test_an_extra_container_is_not_allowed(self):
        self.spec["containers"].append({"name": "debug", "image": SAFE_IMAGE, "securityContext": {
            "allowPrivilegeEscalation": False}})
        [finding] = self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED")
        self.assertEqual(finding["actual"], [self.container["name"], "debug"])

    def test_an_unreviewed_init_container_is_not_allowed(self):
        self.spec["initContainers"] = [{"name": "setup", "image": SAFE_IMAGE,
                                        "securityContext": {"allowPrivilegeEscalation": False}}]
        self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED", path="baseline.pod_template.spec.initContainers[0]")

    def test_a_different_envoy_digest_is_rejected(self):
        self.sidecar["image"] = "envoyproxy/envoy@sha256:" + "f" * 64
        self.assert_finding("SECURITY_SIDECAR_IMAGE", path="baseline.pod_template.spec.containers[1].image")
        self.sidecar["image"] = "envoyproxy/envoy:v1.40.2"
        self.assert_finding("SECURITY_SIDECAR_IMAGE")

    def test_a_host_path_is_rejected(self):
        # The key volume swapped for the node's filesystem: both the host-access and the sidecar rules refuse it.
        self.spec["volumes"][1] = {"name": "upstream", "hostPath": {"path": "/etc/kubernetes/pki"}}
        self.assert_finding("SECURITY_HOST_ACCESS", path="baseline.pod_template.spec.volumes[1].hostPath")
        [finding] = self.assert_finding("SECURITY_SIDECAR_MOUNTS")
        self.assertIn({"volume": "upstream", "mount_path": "/pki/upstream", "source": {"kind": "hostPath"}},
                      finding["actual"])

    def test_a_host_path_is_rejected_even_without_the_host_access_rule(self):
        del self.policy["security_constraints"]["deny_host_access"]
        self.spec["volumes"].append({"name": "node", "hostPath": {"path": "/"}})
        self.sidecar["volumeMounts"].append({"name": "node", "mountPath": "/host", "readOnly": True})
        self.assert_finding("SECURITY_SIDECAR_MOUNTS")

    def test_a_privileged_sidecar_is_rejected(self):
        self.sidecar["securityContext"]["privileged"] = True
        self.assert_finding("SECURITY_PRIVILEGED", path="baseline.pod_template.spec.containers[1].securityContext.privileged")

    def test_another_secret_writable_or_extra_mount_is_rejected(self):
        for change in (
            lambda: self.spec["volumes"][1]["secret"].update(secretName="security-gateway"),
            lambda: self.sidecar["volumeMounts"][1].update(readOnly=False),
            lambda: self.sidecar["volumeMounts"][1].update(subPath="tls.key"),
            lambda: self.sidecar["volumeMounts"].pop(),
            lambda: self.sidecar["volumeMounts"].append({"name": "security-upstream", "mountPath": "/other",
                                                         "readOnly": True}),
            lambda: self.sidecar["volumeMounts"][0].update(mountPath="/etc"),
        ):
            with self.subTest(change=change):
                self.setUp()
                change()
                self.assert_finding("SECURITY_SIDECAR_MOUNTS")

    def test_sidecar_environment_references_are_rejected(self):
        self.sidecar["envFrom"] = [{"secretRef": {"name": "security-gateway"}}]
        self.assert_finding("SECURITY_SIDECAR_ENV")

    def test_a_template_without_the_reviewed_sidecar_is_rejected(self):
        self.spec["containers"].pop()
        self.assert_finding("SECURITY_SIDECAR_MISSING")

    def test_another_targets_sidecar_is_not_allowed(self):
        self.policy["security_constraints"]["allowed_sidecars"][0]["target"] = "cartservice"
        self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED")

    def test_a_renamed_application_container_cannot_pose_as_the_sidecar(self):
        self.spec["containers"].pop()
        self.container["name"] = "shipping-ingress"
        self.assert_finding("SECURITY_SIDECAR_IMAGE")

    def test_a_secret_mount_without_its_reference_version_escalates(self):
        self.live["reference_versions"] = [record for record in self.live["reference_versions"] if record["kind"] != "Secret"]
        self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN")
        self.assertEqual(self.failures(), [])

    def test_malformed_sidecar_rules_are_refused(self):
        entry = self.policy["security_constraints"]["allowed_sidecars"][0]
        mount = entry["mounts"][0]
        for value in (
            "shipping-ingress", [dict(entry, image="envoyproxy/envoy:v1.40.2")], [dict(entry, extra=True)],
            [dict(entry, name="Bad_Name")], [entry, deepcopy(entry)], [dict(entry, mounts="none")],
            [dict(entry, mounts=[dict(mount, source={"kind": "hostPath", "name": "x"})])],
            [dict(entry, mounts=[dict(mount, mount_path="relative")])],
            [dict(entry, mounts=[dict(mount, mount_path="/a/../b")])],
            [dict(entry, mounts=[mount, dict(mount, mount_path="/other")])],
            [dict(entry, mounts=[dict(mount, read_only=False)])],
        ):
            with self.subTest(value=value):
                policy = deepcopy(self.policy)
                policy["security_constraints"]["allowed_sidecars"] = value
                with self.assertRaises(GuardInputError):
                    validate_policy(policy)

    def test_a_reviewed_command_and_arguments_are_pinned_when_named(self):
        entry = self.policy["security_constraints"]["allowed_sidecars"][0]
        entry.update(command=["envoy"], args=["-c", "/config/envoy.json"])
        self.assertEqual([(item["path"].rsplit(".", 1)[-1], item["status"]) for item in self.run_checks()
                          if item["code"] == "SECURITY_SIDECAR_COMMAND"],
                         [("args", "PASS"), ("command", "PASS"), ("containers[1]", "PASS")])
        self.sidecar["args"] = ["--config-yaml", "static_resources: {}"]
        self.assert_finding("SECURITY_SIDECAR_COMMAND", path="baseline.pod_template.spec.containers[1].args")
        for value in ("envoy", [1], None):
            with self.subTest(value=value):
                policy = deepcopy(self.policy)
                policy["security_constraints"]["allowed_sidecars"][0]["command"] = value
                with self.assertRaises(GuardInputError):
                    validate_policy(policy)

    def test_an_empty_review_list_allows_only_the_application_container(self):
        self.policy["security_constraints"]["allowed_sidecars"] = []
        self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED")
        self.spec["containers"].pop()
        self.assert_finding("SECURITY_SIDECAR_NOT_ALLOWED", "PASS")


SAFE_IMAGE = "demo/sentinel@sha256:" + "c" * 64


class ProtectedSidecarDecisionTests(unittest.TestCase):
    """End to end through evaluate(): an extra container in the protected template is a REJECT."""

    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        self.policy["security_constraints"] = {"allowed_sidecars": [{
            "target": self.proposal["target"]["name"], "name": "shipping-ingress", "image": ENVOY, "mounts": []}]}
        sidecar = ingress_sidecar()
        sidecar["volumeMounts"] = []
        self.baseline["pod_template"]["spec"]["containers"].append(sidecar)

    def decide(self):
        self.baseline["template_sha256"] = template_sha256(self.baseline["pod_template"])
        self.proposal["parameters"]["template_sha256"] = self.baseline["template_sha256"]
        approval = {"action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
                    "policy_version": self.policy["policy_version"], "operator_id": "fixture-operator",
                    "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z", "expires_at": "2026-10-10T12:01:35Z"}
        decision = evaluate(self.proposal, self.live, self.policy, self.baseline, self.dependencies, approval, now=NOW)
        validate_message(decision)
        return decision

    def test_the_reviewed_sidecar_is_allowed(self):
        decision = self.decide()
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])

    def test_an_extra_container_is_rejected(self):
        self.baseline["pod_template"]["spec"]["containers"].append({"name": "debug", "image": SAFE_IMAGE})
        decision = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("SECURITY_SIDECAR_NOT_ALLOWED", decision["reason_codes"])

    def test_a_different_envoy_digest_is_rejected(self):
        self.baseline["pod_template"]["spec"]["containers"][1]["image"] = "envoyproxy/envoy@sha256:" + "f" * 64
        decision = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("SECURITY_SIDECAR_IMAGE", decision["reason_codes"])

    def test_a_lifecycle_hook_or_exec_probe_is_rejected(self):
        shell = {"exec": {"command": ["/bin/sh", "-c", "echo pwn"]}}
        for key, value in (("lifecycle", {"postStart": shell}), ("lifecycle", {"preStop": shell}),
                           ("livenessProbe", shell), ("readinessProbe", shell)):
            with self.subTest(key=key):
                self.setUp()
                self.baseline["pod_template"]["spec"]["containers"][1][key] = deepcopy(value)
                decision = self.decide()
                self.assertEqual(decision["verdict"], "REJECT")
                self.assertIn("SECURITY_SIDECAR_COMMAND", decision["reason_codes"])


ISSUER = {"group": "cert-manager.io", "kind": "ClusterIssuer", "name": "sentinel-lab-ca"}


def certificate_record(namespace, **changes):
    """One record of the trusted Certificate status reader (services/guard/adapters.py::certificate_record)."""
    record = {"kind": "Certificate", "namespace": namespace, "name": "security-upstream", "uid": "cert-uid",
              "generation": 2, "secret_name": "security-upstream", "dns_names": ["upstream.internal"],
              "issuer": dict(ISSUER), "ready": "True", "ready_observed_generation": 2,
              "not_before": "2026-10-10T11:00:00Z", "not_after": "2026-10-10T13:00:00Z", "revision": 5}
    record.update(changes)
    return record


def certificate_identity(**changes):
    entry = {"secret": "security-upstream", "certificate": "security-upstream", "issuer": dict(ISSUER),
             "dns_names": ["upstream.internal"], "min_remaining_seconds": 1800}
    entry.update(changes)
    return entry


class CertificateIdentityEligibilityTests(ProtectedSidecarEligibilityTests):
    """P5.06: every P5.05 rule still holds, the Secret is checked through its Certificate, and identity failures escalate."""

    def setUp(self):
        super().setUp()
        namespace = self.proposal["target"]["namespace"]
        self.policy["security_constraints"]["certificate_identities"] = [certificate_identity()]
        # The reader no longer reads the Secret: only the ConfigMap has a reference version.
        for state in (self.baseline, self.live):
            state["reference_versions"] = [record for record in state["reference_versions"] if record["kind"] != "Secret"]
        self.baseline["certificates"] = [certificate_record(namespace, revision=4)]
        self.live["certificates"] = [certificate_record(namespace)]
        self.namespace = namespace

    def identity(self, **changes):
        self.live["certificates"] = [certificate_record(self.namespace, **changes)]

    def test_safe_baseline_passes_all_enabled_checks(self):
        super().test_safe_baseline_passes_all_enabled_checks()
        for code in ("SECURITY_IDENTITY_SPEC", "SECURITY_IDENTITY_NOT_READY", "SECURITY_IDENTITY_EXPIRING",
                     "SECURITY_IDENTITY_NOT_YET_VALID"):
            self.assert_finding(code, "PASS")
        [finding] = [item for item in self.assert_finding("BASELINE_REFERENCE_CHANGED", "PASS",
                                                          path="live_state.certificates")
                     if item["path"] == "live_state.certificates"]
        self.assertEqual(finding["actual"], {"reference": "Secret/" + self.namespace + "/security-upstream",
                                             "certificate": "security-upstream", "uid": "cert-uid", "generation": 2})

    def test_a_renewal_is_not_a_changed_reference(self):
        # A renewal brings a new key, serial, revision and validity, never a new UID or generation.
        self.identity(revision=9, not_before="2026-10-10T12:00:00Z", not_after="2026-10-10T14:00:00Z")
        self.assertEqual(self.failures(), [])
        self.assertEqual(self.failures("UNKNOWN"), [])

    def test_an_expiring_or_expired_identity_escalates(self):
        for not_after, remaining in (("2026-10-10T12:30:00Z", 1760.0), ("2026-10-10T12:00:00Z", -40.0),
                                     ("2026-10-09T12:00:00Z", -86440.0)):
            with self.subTest(not_after=not_after):
                self.identity(not_after=not_after)
                [finding] = self.assert_finding("SECURITY_IDENTITY_EXPIRING", "UNKNOWN",
                                                path="live_state.certificates[security-upstream].not_after")
                self.assertEqual(finding["actual"], {"remaining_seconds": remaining, "revision": 5})
                self.assertEqual(self.failures(), [])
        # The margin boundary itself passes.
        self.identity(not_after="2026-10-10T12:30:40Z")
        self.assert_finding("SECURITY_IDENTITY_EXPIRING", "PASS")

    def test_a_certificate_that_is_not_ready_escalates(self):
        for changes in ({"ready": "False"}, {"ready": "Unknown"}, {"ready_observed_generation": 1},
                        {"ready_observed_generation": None}):
            with self.subTest(changes=changes):
                self.identity(**changes)
                self.assert_finding("SECURITY_IDENTITY_NOT_READY", "UNKNOWN")
                self.assertEqual(self.failures(), [])

    def test_a_certificate_without_a_validity_period_or_not_yet_valid_escalates(self):
        self.identity(not_after=None)
        self.assert_finding("SECURITY_IDENTITY_EXPIRING", "UNKNOWN")
        self.identity(not_before="2026-10-10T12:05:00Z")
        self.assert_finding("SECURITY_IDENTITY_NOT_YET_VALID", "UNKNOWN")

    def test_another_issuer_secret_or_name_is_not_the_reviewed_identity(self):
        for changes in ({"issuer": dict(ISSUER, name="sentinel-untrusted-ca")}, {"issuer": dict(ISSUER, kind="Issuer")},
                        {"dns_names": ["wrong.internal"]}, {"dns_names": ["upstream.internal", "extra.internal"]},
                        {"secret_name": "security-gateway"}):
            with self.subTest(changes=changes):
                self.identity(**changes)
                self.assert_finding("SECURITY_IDENTITY_SPEC", "UNKNOWN")
        self.identity(dns_names=["upstream.internal"])
        self.assert_finding("SECURITY_IDENTITY_SPEC", "PASS")

    def test_missing_certificate_evidence_escalates(self):
        for live in (None, [], [certificate_record("other-namespace")]):
            with self.subTest(live=live):
                self.setUp()
                if live is None:
                    del self.live["certificates"]
                else:
                    self.live["certificates"] = live
                self.assert_finding("SECURITY_IDENTITY_EVIDENCE_MISSING", "UNKNOWN")
                self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN", path="baseline.certificates")
                self.assertEqual(self.failures(), [])

    def test_a_secret_mount_without_its_reference_version_escalates(self):
        # The Secret has no version record at all now; its Certificate's saved record is what is missing here.
        del self.baseline["certificates"]
        self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN")
        self.assertEqual(self.failures(), [])

    def test_a_replaced_or_respecified_certificate_is_a_changed_reference(self):
        for changes in ({"uid": "other-uid"}, {"generation": 3, "ready_observed_generation": 3}):
            with self.subTest(changes=changes):
                self.identity(**changes)
                self.assert_finding("BASELINE_REFERENCE_CHANGED", path="live_state.certificates")

    def test_malformed_certificate_status_is_invalid(self):
        good = certificate_record(self.namespace)
        for value in ("status", [dict(good, extra=True)], [dict(good, kind="Secret")], [dict(good, generation=0)],
                      [dict(good, ready="yes")], [dict(good, not_after="tomorrow")], [dict(good, dns_names="a")],
                      [dict(good, issuer={"name": "x"})], [good, deepcopy(good)], [dict(good, revision=-1)]):
            with self.subTest(value=value):
                self.live["certificates"] = value
                findings = self.assert_finding("BASELINE_ELIGIBILITY_INVALID", path="live_state.certificates")
                self.assertEqual({item["actual"] for item in findings}, {"malformed"})

    def test_without_the_rule_the_secret_is_still_compared_by_its_own_version(self):
        # A policy without certificate_identities keeps the P5.05 Secret comparison exactly.
        del self.policy["security_constraints"]["certificate_identities"]
        self.assert_finding("BASELINE_REFERENCE_EVIDENCE_MISSING", "UNKNOWN", path="baseline.reference_versions")
        self.assertFalse([item for item in self.run_checks() if item["code"].startswith("SECURITY_IDENTITY")])

    def test_an_identity_the_template_does_not_mount_is_skipped(self):
        self.policy["security_constraints"]["certificate_identities"] = [certificate_identity(
            secret="security-gateway", certificate="security-gateway", dns_names=["gateway.internal"])]
        self.live["reference_versions"] = deepcopy(self.baseline["reference_versions"])
        self.assert_finding("SECURITY_IDENTITIES", "SKIP")

    def test_malformed_certificate_rules_are_refused(self):
        entry = certificate_identity()
        for value in ([], "security-upstream", [dict(entry, extra=True)], [dict(entry, secret="Bad_Name")],
                      [dict(entry, issuer={"name": "x"})], [dict(entry, dns_names=[])],
                      [dict(entry, dns_names=["a.internal", "a.internal"])], [dict(entry, min_remaining_seconds=299)],
                      [dict(entry, min_remaining_seconds=True)], [entry, dict(entry, certificate="other")]):
            with self.subTest(value=value):
                policy = deepcopy(self.policy)
                policy["security_constraints"]["certificate_identities"] = value
                with self.assertRaises(GuardInputError):
                    validate_policy(policy)


class CertificateIdentityDecisionTests(unittest.TestCase):
    """End to end through evaluate(): an identity that cannot serve gives ESCALATE, never ALLOW (P5.06)."""

    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        namespace = self.proposal["target"]["namespace"]
        self.policy["security_constraints"] = {"certificate_identities": [certificate_identity()]}
        spec = self.baseline["pod_template"]["spec"]
        spec["volumes"] = [{"name": "upstream", "secret": {"secretName": "security-upstream"}}]
        self.live["certificates"] = [certificate_record(namespace)]
        self.namespace = namespace

    def decide(self):
        self.baseline["template_sha256"] = template_sha256(self.baseline["pod_template"])
        self.proposal["parameters"]["template_sha256"] = self.baseline["template_sha256"]
        approval = {"action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
                    "policy_version": self.policy["policy_version"], "operator_id": "fixture-operator",
                    "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z", "expires_at": "2026-10-10T12:01:35Z"}
        decision = evaluate(self.proposal, self.live, self.policy, self.baseline, self.dependencies, approval, now=NOW)
        validate_message(decision)
        return decision

    def test_a_ready_identity_with_time_left_is_allowed(self):
        decision = self.decide()
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])

    def test_an_expiring_or_not_ready_identity_escalates_even_when_approved(self):
        for changes, code in (({"not_after": "2026-10-10T12:10:00Z"}, "SECURITY_IDENTITY_EXPIRING"),
                              ({"not_after": "2026-10-10T11:50:00Z"}, "SECURITY_IDENTITY_EXPIRING"),
                              ({"ready": "False"}, "SECURITY_IDENTITY_NOT_READY"),
                              ({"issuer": dict(ISSUER, name="sentinel-untrusted-ca")}, "SECURITY_IDENTITY_SPEC")):
            with self.subTest(code=code, changes=changes):
                self.live["certificates"] = [certificate_record(self.namespace, **changes)]
                decision = self.decide()
                self.assertEqual(decision["verdict"], "ESCALATE")
                self.assertIn(code, decision["reason_codes"])

    def test_missing_status_escalates_and_malformed_status_is_rejected(self):
        del self.live["certificates"]
        self.assertEqual(self.decide()["verdict"], "ESCALATE")
        self.live["certificates"] = [dict(certificate_record(self.namespace), ready=True)]
        decision = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("BASELINE_ELIGIBILITY_INVALID", decision["reason_codes"])


TRIVY = "aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"
SAFE = "demo/sentinel@sha256:" + "a" * 64
WORSE = "demo/sentinel@sha256:" + "b" * 64


def image_scan(image, *findings):
    return {"image": image, "status": "FINDINGS" if findings else "NO_KNOWN_CVES", "scanner_image": TRIVY,
            "scanned_at": "2026-10-10T11:00:00Z",
            "findings": [{"id": cve, "severity": severity} for cve, severity in findings]}


class ImageGateEligibilityTests(EligibilityTests):
    """The Guard's CVE gate (P5.02) over live_state.image_scans, from the trusted reader only."""

    def setUp(self):
        super().setUp()
        self.policy["security_constraints"]["image_vulnerability_gate"] = {
            "block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression", "max_scan_age_seconds": 86400,
            "trusted_scanner_images": [TRIVY], "unknown_result": "ESCALATE"}
        # A config-only rollback: the current template runs the baseline's digest.
        self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = SAFE
        self.live["image_scans"] = {"scans": [image_scan(SAFE, ("CVE-1", "HIGH"))], "exceptions": []}

    def gate_findings(self):
        return [item for item in self.run_checks() if item["code"].startswith("IMAGE_")]

    def test_safe_baseline_passes_all_enabled_checks(self):
        super().test_safe_baseline_passes_all_enabled_checks()
        self.assert_finding("IMAGE_CVE_INHERITED", "PASS", "baseline.pod_template.spec.containers[0].image")

    def test_absent_scans_escalate_never_allow(self):
        del self.live["image_scans"]
        [finding] = self.gate_findings()
        self.assertEqual((finding["code"], finding["status"]), ("IMAGE_SCANS_MISSING", "UNKNOWN"))
        self.policy["security_constraints"]["image_vulnerability_gate"]["unknown_result"] = "REJECT"
        self.assert_finding("IMAGE_SCANS_MISSING", "FAIL")

    def test_a_crafted_worse_baseline_is_a_regression(self):
        self.container["image"] = WORSE
        self.live["image_scans"]["scans"].append(image_scan(WORSE, ("CVE-1", "HIGH"), ("CVE-9", "CRITICAL")))
        [finding] = [item for item in self.gate_findings() if item["status"] == "FAIL"]
        self.assertEqual(finding["code"], "IMAGE_CVE_REGRESSION")
        self.assertEqual(finding["actual"]["ids"], ["CVE-9"])

    def test_every_init_container_image_is_gated(self):
        self.spec["initContainers"] = [{"name": "setup", "image": WORSE}]
        self.assert_finding("IMAGE_SCAN_MISSING", "UNKNOWN", "baseline.pod_template.spec.initContainers[0].image")

    def test_an_unreadable_current_template_cannot_show_inheritance(self):
        self.live["deployment"]["spec"]["template"] = "malformed"
        self.assert_finding("IMAGE_CURRENT_SCAN_UNAVAILABLE", "UNKNOWN")

    def test_malformed_scans_are_invalid_evidence(self):
        for value in ("malformed", {"scans": [image_scan("demo/sentinel:latest")]}, {"scans": [], "exceptions": "x"}):
            with self.subTest(value=value):
                self.live["image_scans"] = value
                self.assert_finding("BASELINE_ELIGIBILITY_INVALID", "FAIL", "live_state.image_scans")

    def test_duplicate_container_names_cannot_hide_an_image(self):
        self.spec["initContainers"] = [{"name": self.container["name"], "image": WORSE}]
        self.assert_finding("BASELINE_ELIGIBILITY_INVALID", "FAIL", "baseline.pod_template.spec")

    def test_unknown_or_weakened_gate_rules_are_refused(self):
        gate_rules = self.policy["security_constraints"]["image_vulnerability_gate"]
        for key, value in (("extra", True), ("block_severities", ["HIGH"]), ("mode", "off"),
                           ("trusted_scanner_images", ["aquasec/trivy:latest"])):
            with self.subTest(key=key):
                policy = deepcopy(self.policy)
                policy["security_constraints"]["image_vulnerability_gate"] = dict(gate_rules, **{key: value})
                with self.assertRaises(GuardInputError):
                    validate_policy(policy)

    def test_restart_is_gated_and_scale_is_not(self):
        del self.live["image_scans"]
        self.proposal["action"] = "RESTART"
        self.assert_finding("IMAGE_SCANS_MISSING", "UNKNOWN")
        self.proposal["action"] = "SCALE"
        self.assertEqual(self.gate_findings(), [])


class ImageGateDecisionTests(unittest.TestCase):
    """End to end through evaluate(): absent scans ESCALATE; usable scans keep an approved rollback ALLOWed."""

    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        self.policy["security_constraints"] = {"image_vulnerability_gate": {
            "block_severities": ["CRITICAL", "HIGH"], "mode": "non_regression", "max_scan_age_seconds": 86400,
            "trusted_scanner_images": [TRIVY]}}
        self.approval = {"action_id": self.proposal["action_id"], "proposal_sha256": proposal_fingerprint(self.proposal),
                         "policy_version": self.policy["policy_version"], "operator_id": "fixture-operator",
                         "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z",
                         "expires_at": "2026-10-10T12:01:35Z"}

    def decide(self):
        return evaluate(self.proposal, self.live, self.policy, self.baseline, self.dependencies,
                             self.approval, now=NOW)

    def test_absent_scans_give_escalate(self):
        decision = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertIn("IMAGE_SCANS_MISSING", decision["reason_codes"])

    def test_a_tagged_baseline_cannot_pass_the_gate(self):
        self.live["image_scans"] = {"scans": [], "exceptions": []}
        decision = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertEqual(decision["reason_codes"], ["IMAGE_SCAN_MISSING"])

    def test_usable_scans_keep_the_approved_rollback_allowed(self):
        for template in (self.baseline["pod_template"], self.live["deployment"]["spec"]["template"]):
            template["spec"]["containers"][0]["image"] = SAFE
        self.baseline["template_sha256"] = template_sha256(self.baseline["pod_template"])
        self.proposal["parameters"]["template_sha256"] = self.baseline["template_sha256"]
        self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)
        self.live["image_scans"] = {"scans": [image_scan(SAFE, ("CVE-1", "HIGH"))], "exceptions": []}
        decision = self.decide()
        validate_message(decision)
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])


if __name__ == "__main__":
    unittest.main()
