"""Bound decisions expire and cannot be replayed with changed trusted inputs."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guard.core import (
    GuardInputError, assert_decision_usable, evaluate_with_report,
    proposal_fingerprint, template_sha256,
)
from services.guard.common import json_sha256
from services.guard.decisions import validate_policy
from shared.contracts import ContractError, validate_message


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class GuardDecisionTests(unittest.TestCase):
    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        self.approval = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.policy["policy_version"],
            "operator_id": "trusted-human-reviewer",
            "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z",
        }
        self.preview = None

    def args(self):
        return (self.proposal, self.live, self.policy, self.baseline,
                self.dependencies, self.approval)

    def decide(self):
        return evaluate_with_report(*self.args(), now=NOW, preview=self.preview)

    def allow(self):
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "ALLOW", decision)
        validate_message(decision)
        json.dumps(report, allow_nan=False)
        return decision, report

    def usable(self, decision, now=NOW):
        return assert_decision_usable(decision, *self.args(), now=now, preview=self.preview)

    def bind_template(self):
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        if "health_evidence" in self.baseline:
            self.baseline["health_evidence"]["template_sha256"] = digest
        if self.approval is not None:
            self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)

    def require_preview(self):
        self.policy["require_server_dry_run"] = True
        self.policy["max_server_dry_run_age_seconds"] = 10
        digest = template_sha256(self.baseline["pod_template"])
        self.preview = {
            "status": "PASSED", "proposal_sha256": proposal_fingerprint(self.proposal),
            "target": deepcopy(self.proposal["target"]),
            "resource_version": self.live["deployment"]["metadata"]["resourceVersion"],
            "template_sha256": digest, "admitted_template_sha256": digest,
            "checked_at": stamp(NOW), "details_code": "ADMISSION_PASSED",
        }

    def make_live_profile(self):
        self.policy.update({
            "profile": "live", "decision_ttl_seconds": 20,
            "max_approval_lifetime_seconds": 60,
            "max_state_age_seconds": 30, "max_dependency_age_seconds": 30,
            "baseline_constraints": {
                "max_age_seconds": 300, "min_healthy_duration_seconds": 30,
                "trusted_sources": ["sentinel-verifier"], "require_health_evidence": True,
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
        self.live["deployment"]["spec"]["template"] = deepcopy(self.baseline["pod_template"])
        self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["env"] = [
            {"name": "PORT", "value": "invalid-port"}
        ]
        self.baseline["reference_versions"] = []
        self.live["reference_versions"] = []
        self.baseline["health_evidence"] = {
            "source": "sentinel-verifier", "evidence_id": "healthy-window-1", "status": "HEALTHY",
            "target": deepcopy(self.proposal["target"]),
            "template_sha256": self.baseline["template_sha256"],
            "window_start": "2026-10-10T11:58:30Z",
            "window_end": self.baseline["healthy_observed_at"],
        }
        self.approval["expires_at"] = stamp(NOW + timedelta(seconds=20))
        self.bind_template()

    def test_v2_allow_binds_exact_proposal_and_every_input(self):
        decision, report = self.allow()
        self.assertEqual(decision["schema_version"], "2.0")
        self.assertEqual(decision["proposal_sha256"], proposal_fingerprint(self.proposal))
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=30)))
        self.assertEqual(report["decision_sha256"], json_sha256(decision))
        self.assertEqual(report["inputs_sha256"], decision["inputs_sha256"])
        self.assertTrue(self.usable(decision))

    def test_approval_only_reasons_remain_compatible_with_coordinator_routing(self):
        for case, expected in (("missing", "OPERATOR_APPROVAL_REQUIRED"),
                               ("expired", "APPROVAL_EXPIRED"),
                               ("mismatched", "APPROVAL_MISMATCH")):
            with self.subTest(case=case):
                self.setUp()
                if case == "missing":
                    self.approval = None
                elif case == "expired":
                    self.approval["expires_at"] = stamp(NOW - timedelta(seconds=1))
                else:
                    self.approval["policy_version"] = "previous-policy"
                decision, report = self.decide()
                self.assertEqual(decision["verdict"], "ESCALATE")
                self.assertEqual(decision["reason_codes"], [expected])
                self.assertTrue(any(item["code"] == expected and item["status"] == "UNKNOWN"
                                    for item in report["checks"]))

    def test_any_changed_input_prevents_reuse_even_when_semantically_benign(self):
        mutations = (
            ("proposal", lambda: self.proposal.update(rationale="A clarified diagnosis")),
            ("baseline", lambda: self.baseline.update(source="another-trusted-store")),
            ("live_state", lambda: self.live["deployment"]["metadata"].update(generation=3)),
            ("policy", lambda: self.policy.update(note="reviewed policy note changed")),
            ("dependencies", lambda: self.dependencies["affected_services"].append("another-service")),
            ("approval", lambda: self.approval.update(operator_id="other-human-reviewer")),
        )
        for label, mutate in mutations:
            with self.subTest(input=label):
                self.setUp()
                decision, _ = self.allow()
                mutate()
                with self.assertRaises(GuardInputError):
                    self.usable(decision)

    def test_changed_resource_version_and_revoked_approval_prevent_reuse(self):
        decision, _ = self.allow()
        self.live["deployment"]["metadata"]["resourceVersion"] = "103"
        with self.assertRaises(GuardInputError):
            self.usable(decision)
        self.setUp()
        decision, _ = self.allow()
        self.approval["decision"] = "REJECTED"
        with self.assertRaises(GuardInputError):
            self.usable(decision)

    def test_preview_is_included_in_input_binding(self):
        self.require_preview()
        decision, _ = self.allow()
        self.preview["details_code"] = "DIFFERENT_RESULT"
        with self.assertRaises(GuardInputError):
            self.usable(decision)

    def test_expiry_is_exclusive_and_future_decisions_are_unusable(self):
        decision, _ = self.allow()
        self.assertTrue(self.usable(decision, NOW + timedelta(seconds=29, microseconds=999999)))
        for when in (NOW - timedelta(microseconds=1), NOW + timedelta(seconds=30)):
            with self.subTest(when=when):
                with self.assertRaises(GuardInputError):
                    self.usable(decision, when)

    def test_configured_ttl_controls_expiry(self):
        for ttl in (1, 300):
            with self.subTest(ttl=ttl):
                self.policy["decision_ttl_seconds"] = ttl
                decision, _ = self.allow()
                self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=ttl)))

    def test_invalid_ttl_is_an_input_error(self):
        for value in (0, -1, 301, True, "30", None, 1.5):
            with self.subTest(ttl=value):
                self.policy["decision_ttl_seconds"] = value
                with self.assertRaises(GuardInputError):
                    self.decide()

    def test_decision_deadline_is_clipped_to_approval_expiry(self):
        self.approval["expires_at"] = stamp(NOW + timedelta(seconds=7))
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], self.approval["expires_at"])

    def test_decision_deadline_is_clipped_to_state_freshness(self):
        self.policy["max_state_age_seconds"] = 15
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=5)))

    def test_decision_deadline_is_clipped_to_dependency_freshness(self):
        self.policy["max_dependency_age_seconds"] = 10
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=4)))

    def test_decision_deadline_is_clipped_to_preview_freshness(self):
        self.require_preview()
        self.preview["checked_at"] = stamp(NOW - timedelta(seconds=6))
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=4)))

    def test_optional_preview_default_freshness_also_clips_decision(self):
        self.require_preview()
        del self.policy["require_server_dry_run"]
        del self.policy["max_server_dry_run_age_seconds"]
        self.preview["checked_at"] = stamp(NOW - timedelta(seconds=6))
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=4)))

    def test_baseline_eligibility_expiry_also_clips_decision(self):
        self.policy["baseline_constraints"] = {"max_age_seconds": 75}
        # The baseline was healthy 70 seconds ago, leaving five seconds of
        # eligibility even though the general decision lifetime is 30 seconds.
        decision, _ = self.allow()
        self.assertEqual(decision["valid_until"], stamp(NOW + timedelta(seconds=5)))

    def test_rejected_or_escalated_decision_is_never_usable(self):
        for verdict in ("REJECT", "ESCALATE"):
            with self.subTest(verdict=verdict):
                decision, _ = self.allow()
                decision["verdict"] = verdict
                with self.assertRaises(GuardInputError):
                    self.usable(decision)

    def test_legacy_wire_contract_still_valid_but_cannot_authorize_execution(self):
        legacy = fixture("data/sentinel/samples/messages/guard-result.json")
        legacy["verdict"] = "ALLOW"
        validate_message(legacy)
        with self.assertRaises(GuardInputError):
            self.usable(legacy)

    def test_v2_contract_rejects_missing_or_invalid_binding_fields(self):
        decision, _ = self.allow()
        for key in ("proposal_sha256", "inputs_sha256"):
            for value in (None, "a" * 63, "A" * 64, "g" * 64, True):
                with self.subTest(key=key, value=value):
                    changed = deepcopy(decision)
                    changed[key] = value
                    with self.assertRaises(ContractError):
                        validate_message(changed)
            changed = deepcopy(decision)
            del changed[key]
            with self.assertRaises(ContractError):
                validate_message(changed)

    def test_v2_contract_rejects_reversed_deadline(self):
        decision, _ = self.allow()
        decision["valid_until"] = stamp(NOW - timedelta(microseconds=1))
        with self.assertRaises(ContractError):
            validate_message(decision)

    def test_resource_report_identifies_container_path_actual_and_expected(self):
        self.policy["resource_constraints"] = {"max_container_limits": {"memory": "128Mi"}}
        self.baseline["pod_template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] = "256Mi"
        self.bind_template()
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        detail = next(item for item in report["checks"] if item["code"] == "RESOURCE_LIMIT_EXCEEDS_POLICY" and item["path"].endswith("containers[0].resources.limits.memory"))
        self.assertEqual((detail["actual"], detail["expected"]), ("256Mi", "128Mi"))
        self.assertIn("demo-api", detail["message"])

    def test_report_fingerprints_inputs_without_echoing_raw_environment_values(self):
        marker = "private-environment-value-that-must-not-appear-in-audit"
        self.baseline["pod_template"]["spec"]["containers"][0]["env"].append({"name": "PRIVATE_VALUE", "value": marker})
        self.bind_template()
        decision, report = self.allow()
        self.assertNotIn(marker, json.dumps(report))
        self.assertNotIn(marker, json.dumps(decision))
        for key, value in zip(("proposal", "live_state", "policy", "baseline", "dependencies", "approval"), self.args()):
            self.assertEqual(report["input_fingerprints"][key], json_sha256(value))

    def test_missing_health_evidence_escalates_without_fabricating_safety(self):
        self.policy["baseline_constraints"] = {"require_health_evidence": True}
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertIn("BASELINE_HEALTH_EVIDENCE_MISSING", decision["reason_codes"])
        self.assertTrue(any(item["code"] == "BASELINE_HEALTH_EVIDENCE_MISSING" and item["status"] == "UNKNOWN" for item in report["checks"]))

    def test_security_hard_reject_precedes_missing_health_or_approval(self):
        self.policy["baseline_constraints"] = {"require_health_evidence": True}
        self.policy["security_constraints"] = {"deny_host_access": True}
        self.baseline["pod_template"]["spec"]["hostNetwork"] = True
        self.bind_template()
        self.approval = None
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("SECURITY_HOST_ACCESS", decision["reason_codes"])
        self.assertNotIn("OPERATOR_APPROVAL_REQUIRED", decision["reason_codes"])
        self.assertTrue(any(item["status"] == "UNKNOWN" for item in report["checks"]))

    def test_missing_required_preview_escalates(self):
        self.require_preview()
        self.preview = None
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertIn("SERVER_DRY_RUN_REQUIRED", decision["reason_codes"])

    def test_bound_successful_preview_allows(self):
        self.require_preview()
        self.allow()

    def test_mismatched_preview_cannot_allow(self):
        for field, value in (("resource_version", "another-version"), ("proposal_sha256", "b" * 64), ("template_sha256", "b" * 64), ("target", dict(self.proposal["target"], uid="different-uid"))):
            with self.subTest(field=field):
                self.require_preview()
                self.preview[field] = value
                decision, _ = self.decide()
                self.assertEqual(decision["verdict"], "REJECT")
                self.assertIn("SERVER_DRY_RUN_MISMATCH", decision["reason_codes"])

    def test_stale_or_future_preview_escalates(self):
        for delta in (-11, 1):
            with self.subTest(delta=delta):
                self.require_preview()
                self.preview["checked_at"] = stamp(NOW + timedelta(seconds=delta))
                decision, _ = self.decide()
                self.assertEqual(decision["verdict"], "ESCALATE")
                self.assertIn("SERVER_DRY_RUN_STALE", decision["reason_codes"])

    def test_failed_preview_such_as_an_admission_denial_escalates(self):
        # preview.py reports any non-zero kubectl exit, an admission denial
        # included, as UNKNOWN with SERVER_DRY_RUN_FAILED.
        self.require_preview()
        self.preview.update(status="UNKNOWN", details_code="SERVER_DRY_RUN_FAILED", admitted_template_sha256=None)
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertIn("SERVER_DRY_RUN_UNKNOWN", decision["reason_codes"])
        self.assertNotIn("SERVER_DRY_RUN_REJECTED", decision["reason_codes"])

    def test_admission_mutation_or_rejection_is_a_hard_failure(self):
        self.require_preview()
        self.preview["admitted_template_sha256"] = "b" * 64
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("SERVER_DRY_RUN_REJECTED", decision["reason_codes"])
        self.require_preview()
        self.preview["status"] = "REJECTED"
        self.preview["admitted_template_sha256"] = None
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")

    def test_strict_live_profile_can_pass_with_all_trusted_evidence(self):
        self.make_live_profile()
        validate_policy(self.policy, require_live=True)
        self.allow()

    def test_live_profile_missing_safety_fields_is_rejected(self):
        self.make_live_profile()
        original = deepcopy(self.policy)
        paths = (
            ("decision_ttl_seconds",), ("max_approval_lifetime_seconds",),
            ("max_state_age_seconds",), ("max_dependency_age_seconds",),
            ("baseline_constraints", "require_health_evidence"),
            ("baseline_constraints", "require_compatible_references"),
            ("baseline_constraints", "require_image_digests"),
            ("baseline_constraints", "trusted_sources"),
            ("baseline_constraints", "min_healthy_duration_seconds"),
            ("security_constraints", "deny_privileged"),
            ("security_constraints", "deny_host_access"),
            ("security_constraints", "allowed_image_registries"),
            ("security_constraints", "allowed_service_accounts"),
            ("security_constraints", "forbid_service_account_change"),
            ("resource_constraints", "require_requests_and_limits"),
            ("workload_constraints", "max_pod_requests", "memory"),
            ("workload_constraints", "max_rollout_requests", "cpu"),
            ("workload_constraints", "max_replicas"),
            ("workload_constraints", "max_surge_replicas"),
        )
        for path in paths:
            with self.subTest(path=path):
                self.policy = deepcopy(original)
                parent = self.policy
                for part in path[:-1]:
                    parent = parent[part]
                del parent[path[-1]]
                with self.assertRaises(GuardInputError):
                    self.decide()

    def test_live_profile_requires_expiring_bounded_approval(self):
        self.make_live_profile()
        del self.approval["expires_at"]
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "ESCALATE")
        self.assertIn("APPROVAL_EXPIRY_REQUIRED", decision["reason_codes"])
        self.approval["expires_at"] = stamp(NOW + timedelta(seconds=61))
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("APPROVAL_LIFETIME", decision["reason_codes"])

    def test_live_reader_refuses_development_policy(self):
        with self.assertRaises(GuardInputError):
            validate_policy(self.policy, require_live=True)

    def test_evaluation_and_consumption_do_not_mutate_inputs(self):
        before = deepcopy(self.args())
        decision, _ = self.allow()
        self.usable(decision)
        self.assertEqual(self.args(), before)


if __name__ == "__main__":
    unittest.main()
