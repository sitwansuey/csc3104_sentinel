"""Do not present incomplete or contradictory evidence as a successful refusal."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from scripts.demos.demo_guard import load_fixtures
from scripts.demos.guard_rejection_demo import (
    REASON, UNCHANGED_FIELDS, require_valid_approval, validate_rejection_proof,
)
from services.guard.core import evaluate_with_report, proposal_fingerprint


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


class ApprovalClaimTests(unittest.TestCase):
    def setUp(self):
        self.inputs = load_fixtures()
        self.inputs["live_state"]["deployment"]["spec"]["template"] = deepcopy(
            self.inputs["baseline"]["pod_template"])
        self.approval = {
            "action_id": self.inputs["proposal"]["action_id"],
            "proposal_sha256": proposal_fingerprint(self.inputs["proposal"]),
            "policy_version": self.inputs["policy"]["policy_version"],
            "operator_id": "test-operator", "decision": "APPROVED",
            "approved_at": (NOW - timedelta(seconds=5)).isoformat(),
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
        }

    def test_valid_approval_can_be_claimed_alongside_necessity_rejection(self):
        decision, report = evaluate_with_report(**self.inputs, approval=self.approval, now=NOW)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertEqual(decision["reason_codes"], [REASON])
        require_valid_approval(report)

    def test_hard_rejection_cannot_hide_expired_or_mismatched_approval(self):
        for changes in ({"expires_at": (NOW - timedelta(seconds=1)).isoformat()},
                        {"proposal_sha256": "b" * 64}, {"decision": "REJECTED"}):
            with self.subTest(changes=changes):
                approval = dict(self.approval, **changes)
                decision, report = evaluate_with_report(**self.inputs, approval=approval, now=NOW)
                self.assertEqual(decision["verdict"], "REJECT")
                self.assertIn(REASON, decision["reason_codes"])
                with self.assertRaisesRegex(RuntimeError, "valid approval"):
                    require_valid_approval(report)

    def test_pass_marker_alone_cannot_hide_other_approval_failures(self):
        for code in ("APPROVAL_EXPIRED", "APPROVAL_MISMATCH", "OPERATOR_REJECTED"):
            for status in ("FAIL", "UNKNOWN"):
                with self.subTest(code=code, status=status):
                    report = {"checks": [{"code": "APPROVAL_CHECK", "status": "PASS"},
                                         {"code": code, "status": status}]}
                    with self.assertRaises(RuntimeError):
                        require_valid_approval(report)


class RejectionProofTests(unittest.TestCase):
    def setUp(self):
        before = {
            "uid": "shipping-uid", "resource_version": "102", "generation": 3,
            "template_sha256": "a" * 64, "pod_uids": ["shipping-pod-uid"],
            "desired": 1, "ready": 1, "available": 1, "observed_generation": 3,
        }
        self.proof = {
            "incident_id": "INC-false-alarm", "action_id": "ACT-unnecessary",
            "guard_verdict": "REJECT", "approved_guard_verdict": "REJECT",
            "reason_code": REASON, "executor_status": "FAILED",
            "executor_patch_attempts": 0, "executor_journal_attempts": 0, "executor_routes": 0,
            "before": before, "after": deepcopy(before),
            "unchanged": {key: True for key in UNCHANGED_FIELDS},
        }

    def validate(self, proof):
        return validate_rejection_proof(proof, incident_id="INC-false-alarm",
                                        action_id="ACT-unnecessary", target_uid="shipping-uid")

    def test_complete_matching_evidence_passes_without_mutating_inputs(self):
        original = deepcopy(self.proof)
        self.validate(self.proof)
        self.assertEqual(self.proof, original)

    def test_missing_or_incomplete_unchanged_map_cannot_pass_vacuously(self):
        for value in ({}, None, [], {"uid": True},
                      *({key: True for key in UNCHANGED_FIELDS if key != omitted}
                        for omitted in UNCHANGED_FIELDS)):
            with self.subTest(value=value):
                proof = dict(self.proof, unchanged=value)
                with self.assertRaisesRegex(RuntimeError, "incomplete"):
                    self.validate(proof)
        proof = deepcopy(self.proof)
        del proof["unchanged"]
        with self.assertRaises(RuntimeError):
            self.validate(proof)

    def test_unchanged_flags_require_true_not_truthy_or_false_values(self):
        for value in (1, "true", False, None):
            with self.subTest(value=value):
                proof = deepcopy(self.proof)
                proof["unchanged"]["generation"] = value
                with self.assertRaises(RuntimeError):
                    self.validate(proof)

    def test_proof_must_belong_to_the_selected_incident_action_and_target(self):
        for key in ("incident_id", "action_id"):
            with self.subTest(key=key):
                proof = dict(self.proof, **{key: "another-run"})
                with self.assertRaises(RuntimeError):
                    self.validate(proof)
        proof = deepcopy(self.proof)
        proof["before"]["uid"] = proof["after"]["uid"] = "another-target"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            self.validate(proof)

    def test_zero_attempt_claim_requires_present_integer_zero_counters(self):
        for key in ("executor_patch_attempts", "executor_journal_attempts", "executor_routes"):
            for value in (1, -1, 0.0, False, "0", None):
                with self.subTest(key=key, value=value):
                    proof = dict(self.proof, **{key: value})
                    with self.assertRaisesRegex(RuntimeError, "zero counters"):
                        self.validate(proof)
            proof = deepcopy(self.proof)
            del proof[key]
            with self.assertRaises(RuntimeError):
                self.validate(proof)

    def test_true_flags_cannot_hide_changed_before_and_after_evidence(self):
        changed = {"uid": "other-uid", "resource_version": "103", "generation": 4,
                   "template_sha256": "b" * 64, "pod_uids": ["replacement-pod"]}
        for key, value in changed.items():
            with self.subTest(key=key):
                proof = deepcopy(self.proof)
                proof["after"][key] = value
                with self.assertRaisesRegex(RuntimeError, "identity or configuration"):
                    self.validate(proof)

    def test_missing_before_or_after_fields_cannot_prove_unchanged_workload(self):
        for side in ("before", "after"):
            for key in UNCHANGED_FIELDS:
                with self.subTest(side=side, key=key):
                    proof = deepcopy(self.proof)
                    del proof[side][key]
                    with self.assertRaises(RuntimeError):
                        self.validate(proof)

    def test_both_health_samples_require_positive_integer_desired_replicas(self):
        for side in ("before", "after"):
            for count in (0, -1, False, 1.0, "1", None):
                with self.subTest(side=side, count=count):
                    proof = deepcopy(self.proof)
                    proof[side].update(desired=count, ready=count, available=count)
                    with self.assertRaisesRegex(RuntimeError, "remain ready"):
                        self.validate(proof)
            for field in ("desired", "ready", "available"):
                proof = deepcopy(self.proof)
                del proof[side][field]
                with self.subTest(side=side, missing=field), self.assertRaises(RuntimeError):
                    self.validate(proof)

    def test_replica_shortfall_or_different_refusal_invalidates_proof(self):
        for side in ("before", "after"):
            for key in ("ready", "available"):
                proof = deepcopy(self.proof)
                proof[side][key] = 0
                with self.subTest(side=side, key=key), self.assertRaises(RuntimeError):
                    self.validate(proof)
        for key, value in (("guard_verdict", "ALLOW"), ("approved_guard_verdict", "ALLOW"),
                           ("reason_code", "OPERATOR_APPROVAL_REQUIRED"), ("executor_status", "APPLIED")):
            with self.subTest(key=key):
                with self.assertRaises(RuntimeError):
                    self.validate(dict(self.proof, **{key: value}))


if __name__ == "__main__":
    unittest.main()
