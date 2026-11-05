"""Safety decisions exercised offline against the team's development fixtures."""

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services.guard.core import GuardInputError, evaluate, proposal_fingerprint, template_sha256
from shared.contracts import ContractError, validate_message


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class GuardDecisionTests(unittest.TestCase):
    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live_state = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")

    def approval(self, **changes):
        record = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.policy["policy_version"],
            "operator_id": "fixture-operator",
            "decision": "APPROVED",
            "approved_at": "2026-10-10T12:00:35Z",
            "expires_at": "2026-10-10T12:01:35Z",
        }
        record.update(changes)
        return record

    def run_guard(self, approval=None, **changes):
        inputs = {
            "proposal": self.proposal,
            "live_state": self.live_state,
            "policy": self.policy,
            "baseline": self.baseline,
            "dependencies": self.dependencies,
            "approval": approval,
            "now": NOW,
        }
        inputs.update(changes)
        return evaluate(**inputs)

    def assert_decision(self, result, verdict, reason):
        validate_message(result)
        self.assertEqual(result["verdict"], verdict)
        self.assertIn(reason, result["reason_codes"])
        self.assertEqual(result["incident_id"], self.proposal["incident_id"])
        self.assertEqual(result["action_id"], self.proposal["action_id"])
        self.assertEqual(result["target"], self.proposal["target"])
        self.assertEqual(result["policy_version"], self.policy["policy_version"])

    def test_approved_rollback_allows_and_returns_validated_snapshot(self):
        result = self.run_guard(self.approval())
        self.assert_decision(result, "ALLOW", "VALIDATION_PASSED")
        self.assertEqual(result["validated_resource_version"], "102")
        checked_at = datetime.fromisoformat(result["checked_at"].replace("Z", "+00:00"))
        self.assertEqual(checked_at, NOW)

    def test_missing_required_approval_escalates(self):
        self.assert_decision(self.run_guard(), "ESCALATE", "OPERATOR_APPROVAL_REQUIRED")

    def test_policy_can_allow_without_operator_approval(self):
        self.policy["require_operator_approval"] = False
        self.assert_decision(self.run_guard(), "ALLOW", "VALIDATION_PASSED")

    def test_every_target_identity_field_must_match_policy(self):
        for field in ("cluster_id", "namespace", "name", "uid"):
            with self.subTest(field=field):
                policy = copy.deepcopy(self.policy)
                policy["targets"][0][field] = "different-identity"
                self.assert_decision(
                    self.run_guard(self.approval(), policy=policy),
                    "REJECT", "TARGET_NOT_ALLOWED",
                )

    def test_empty_target_or_action_allowlist_rejects(self):
        for field, reason in (("targets", "TARGET_NOT_ALLOWED"),
                              ("allowed_actions", "ACTION_NOT_ALLOWED")):
            with self.subTest(field=field):
                policy = copy.deepcopy(self.policy)
                policy[field] = []
                self.assert_decision(self.run_guard(self.approval(), policy=policy),
                                     "REJECT", reason)

    def test_scale_is_forbidden_under_current_policy(self):
        self.proposal["action"] = "SCALE"
        self.proposal["parameters"] = {"desired_replicas": 3}
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "ACTION_NOT_ALLOWED")

    def test_allowlisting_an_unimplemented_action_does_not_grant_permission(self):
        self.proposal["action"] = "SCALE"
        self.proposal["parameters"] = {"desired_replicas": 3}
        self.policy["allowed_actions"].append("SCALE")
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "ACTION_NOT_IMPLEMENTED")

    def test_live_target_identity_mismatch_rejects(self):
        mutations = (
            ("cluster_id", "other-cluster"),
            ("namespace", "other-namespace"),
            ("name", "other-deployment"),
            ("kind", "StatefulSet"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                live = copy.deepcopy(self.live_state)
                if field == "cluster_id":
                    live[field] = value
                elif field == "kind":
                    live["deployment"][field] = value
                else:
                    live["deployment"]["metadata"][field] = value
                self.assert_decision(self.run_guard(self.approval(), live_state=live),
                                     "REJECT", "LIVE_TARGET_MISMATCH")

    def test_recreated_deployment_with_same_name_rejects(self):
        self.live_state["deployment"]["metadata"]["uid"] = "replacement-uid"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "TARGET_UID_MISMATCH")

    def test_resource_version_change_rejects_even_with_approval(self):
        self.live_state["deployment"]["metadata"]["resourceVersion"] = "103"
        result = self.run_guard(self.approval())
        self.assert_decision(result, "REJECT", "STALE_RESOURCE_VERSION")
        self.assertEqual(result["validated_resource_version"], "103")

    def test_resource_versions_are_compared_as_opaque_strings(self):
        self.proposal["based_on_resource_version"] = "opaque/version:0102"
        self.live_state["deployment"]["metadata"]["resourceVersion"] = "opaque/version:0102"
        self.assert_decision(self.run_guard(self.approval()), "ALLOW", "VALIDATION_PASSED")
        self.live_state["deployment"]["metadata"]["resourceVersion"] = "opaque/version:102"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "STALE_RESOURCE_VERSION")

    def test_revalidation_catches_changes_after_an_initial_allow(self):
        approval = self.approval()
        self.assert_decision(self.run_guard(approval), "ALLOW", "VALIDATION_PASSED")
        self.live_state["deployment"]["metadata"]["resourceVersion"] = "103"
        self.assert_decision(self.run_guard(approval), "REJECT", "STALE_RESOURCE_VERSION")

    def test_live_snapshot_age_limit_accepts_boundary_and_rejects_older(self):
        self.policy["max_state_age_seconds"] = 10
        self.assert_decision(self.run_guard(self.approval()), "ALLOW", "VALIDATION_PASSED")
        self.live_state["captured_at"] = "2026-10-10T12:00:29Z"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "STALE_LIVE_STATE")

    def test_live_snapshot_from_future_rejects(self):
        self.live_state["captured_at"] = "2026-10-10T12:00:41Z"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "LIVE_STATE_FROM_FUTURE")

    def test_missing_or_unreadable_live_resource_version_raises(self):
        for value in (None, "", 102):
            with self.subTest(value=value):
                live = copy.deepcopy(self.live_state)
                live["deployment"]["metadata"]["resourceVersion"] = value
                with self.assertRaises(GuardInputError):
                    self.run_guard(live_state=live)
        with self.assertRaises(GuardInputError):
            self.run_guard(live_state=None)
        del self.live_state["deployment"]["metadata"]["resourceVersion"]
        with self.assertRaises(GuardInputError):
            self.run_guard()

    def test_missing_or_malformed_baseline_rejects(self):
        for baseline, reason in ((None, "BASELINE_MISSING"),
                                 ({}, "INVALID_BASELINE")):
            with self.subTest(baseline=baseline):
                self.assert_decision(self.run_guard(self.approval(), baseline=baseline),
                                     "REJECT", reason)

    def test_baseline_target_and_id_must_match_proposal(self):
        baseline = copy.deepcopy(self.baseline)
        baseline["target"]["uid"] = "replacement-uid"
        self.assert_decision(self.run_guard(self.approval(), baseline=baseline),
                             "REJECT", "BASELINE_TARGET_MISMATCH")
        self.baseline["baseline_id"] = "OTHER-BASELINE"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "BASELINE_ID_MISMATCH")

    def test_modified_template_rejects_despite_unchanged_claimed_digest(self):
        self.baseline["pod_template"]["spec"]["containers"][0]["image"] = "unapproved-image:v2"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "BASELINE_HASH_MISMATCH")

    def test_saved_and_proposed_hashes_must_both_match_actual_template(self):
        for field in ("saved", "proposed"):
            with self.subTest(field=field):
                baseline = copy.deepcopy(self.baseline)
                proposal = copy.deepcopy(self.proposal)
                if field == "saved":
                    baseline["template_sha256"] = "0" * 64
                else:
                    proposal["parameters"]["template_sha256"] = "0" * 64
                self.assert_decision(
                    self.run_guard(self.approval(), baseline=baseline, proposal=proposal),
                    "REJECT", "BASELINE_HASH_MISMATCH",
                )

    def test_baseline_cannot_claim_future_health(self):
        self.baseline["healthy_observed_at"] = "2026-10-10T12:00:41Z"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "BASELINE_FROM_FUTURE")

    def test_dependency_result_must_correlate_to_proposal(self):
        for field in ("incident_id", "action_id", "target"):
            with self.subTest(field=field):
                dependencies = copy.deepcopy(self.dependencies)
                if field == "target":
                    dependencies[field]["uid"] = "different-uid"
                else:
                    dependencies[field] = "different-id"
                self.assert_decision(self.run_guard(self.approval(), dependencies=dependencies),
                                     "REJECT", "DEPENDENCY_RESULT_MISMATCH")

    def test_blocked_dependencies_reject_even_with_operator_approval(self):
        self.dependencies["status"] = "BLOCKED"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "DEPENDENCY_BLOCKED")

    def test_blocker_list_cannot_be_hidden_behind_clear_status(self):
        self.dependencies["blockers"] = ["database migration still running"]
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "DEPENDENCY_BLOCKED")

    def test_unknown_dependencies_follow_policy_and_approval_cannot_override(self):
        for unknown_status in (True, False):
            for policy_verdict in ("ESCALATE", "REJECT"):
                with self.subTest(status_unknown=unknown_status, policy=policy_verdict):
                    dependencies = copy.deepcopy(self.dependencies)
                    if unknown_status:
                        dependencies["status"] = "UNKNOWN"
                    else:
                        dependencies["unknowns"] = ["database state unavailable"]
                    self.policy["unknown_dependency_result"] = policy_verdict
                    self.assert_decision(self.run_guard(self.approval(), dependencies=dependencies),
                                         policy_verdict, "DEPENDENCY_UNKNOWN")

    def test_dependency_age_limit_accepts_boundary_and_uses_unknown_policy_for_older(self):
        self.policy["max_dependency_age_seconds"] = 6
        self.assert_decision(self.run_guard(self.approval()), "ALLOW", "VALIDATION_PASSED")
        self.dependencies["checked_at"] = "2026-10-10T12:00:33Z"
        self.assert_decision(self.run_guard(self.approval()), "ESCALATE", "STALE_DEPENDENCY_RESULT")
        self.policy["unknown_dependency_result"] = "REJECT"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "STALE_DEPENDENCY_RESULT")

    def test_future_dependency_result_follows_unknown_policy(self):
        self.dependencies["checked_at"] = "2026-10-10T12:00:41Z"
        self.assert_decision(self.run_guard(self.approval()), "ESCALATE", "DEPENDENCY_RESULT_FROM_FUTURE")
        self.policy["unknown_dependency_result"] = "REJECT"
        self.assert_decision(self.run_guard(self.approval()), "REJECT", "DEPENDENCY_RESULT_FROM_FUTURE")

    def test_hard_violation_takes_precedence_over_dependency_escalation(self):
        self.live_state["deployment"]["metadata"]["resourceVersion"] = "103"
        self.dependencies["status"] = "UNKNOWN"
        self.assert_decision(self.run_guard(), "REJECT", "STALE_RESOURCE_VERSION")

    def test_approval_is_bound_to_action_policy_and_full_proposal(self):
        changes = (
            {"action_id": "another-action"},
            {"policy_version": "another-policy-version"},
            {"proposal_sha256": "0" * 64},
        )
        for change in changes:
            with self.subTest(change=change):
                self.assert_decision(self.run_guard(self.approval(**change)),
                                     "ESCALATE", "APPROVAL_MISMATCH")

    def test_changing_proposal_after_approval_requires_new_approval(self):
        approval = self.approval()
        self.proposal["rationale"] = "changed rationale after review"
        self.assert_decision(self.run_guard(approval), "ESCALATE", "APPROVAL_MISMATCH")

    def test_matching_operator_denial_rejects(self):
        self.assert_decision(self.run_guard(self.approval(decision="REJECTED")),
                             "REJECT", "OPERATOR_REJECTED")

    def test_denial_for_different_proposal_cannot_be_applied(self):
        self.assert_decision(
            self.run_guard(self.approval(decision="REJECTED", proposal_sha256="0" * 64)),
            "ESCALATE", "APPROVAL_MISMATCH",
        )

    def test_matching_denial_remains_a_veto_when_approval_is_optional(self):
        self.policy["require_operator_approval"] = False
        self.assert_decision(self.run_guard(self.approval(decision="REJECTED")),
                             "REJECT", "OPERATOR_REJECTED")

    def test_expired_or_future_approval_escalates(self):
        for change, reason in (
            ({"expires_at": "2026-10-10T12:00:40Z"}, "APPROVAL_EXPIRED"),
            ({"approved_at": "2026-10-10T12:00:41Z"}, "APPROVAL_FROM_FUTURE"),
        ):
            with self.subTest(change=change):
                self.assert_decision(self.run_guard(self.approval(**change)), "ESCALATE", reason)

    def test_approval_without_optional_expiry_can_allow(self):
        approval = self.approval()
        del approval["expires_at"]
        self.assert_decision(self.run_guard(approval), "ALLOW", "VALIDATION_PASSED")

    def test_malformed_approval_cannot_authorize_recovery(self):
        invalid = []
        for field in ("action_id", "proposal_sha256", "policy_version", "operator_id", "decision", "approved_at"):
            record = self.approval()
            del record[field]
            invalid.append(record)
        invalid.extend([
            self.approval(decision="ALLOW"),
            self.approval(operator_id=""),
            self.approval(approved_at="2026-10-10T12:00:35"),
            self.approval(proposal_sha256="invalid-digest"),
            self.approval(unexpected_field="extra"),
        ])
        for record in invalid:
            with self.subTest(record=record):
                self.assert_decision(self.run_guard(record), "ESCALATE", "INVALID_APPROVAL")

    def test_approval_expiry_is_rechecked_on_later_evaluation(self):
        approval = self.approval(expires_at="2026-10-10T12:00:41Z")
        self.assert_decision(self.run_guard(approval), "ALLOW", "VALIDATION_PASSED")
        self.assert_decision(self.run_guard(approval, now=NOW + timedelta(seconds=1)),
                             "ESCALATE", "APPROVAL_EXPIRED")

    def test_malformed_policy_raises_input_error(self):
        cases = [
            {"require_operator_approval": "false"},
            {"unknown_dependency_result": "ALLOW"},
            {"allowed_actions": ["SHELL"]},
            {"targets": [{}]},
            {"max_state_age_seconds": -1},
            {"max_state_age_seconds": True},
            {"max_dependency_age_seconds": 1.5},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                policy = copy.deepcopy(self.policy)
                policy.update(changes)
                with self.assertRaises(GuardInputError):
                    self.run_guard(policy=policy)
        del self.policy["policy_version"]
        with self.assertRaises(GuardInputError):
            self.run_guard()

    def test_wrong_wire_message_kinds_and_malformed_messages_raise(self):
        with self.assertRaises(ContractError):
            self.run_guard(proposal=self.dependencies)
        with self.assertRaises(ContractError):
            self.run_guard(dependencies=self.proposal)
        proposal = copy.deepcopy(self.proposal)
        proposal["parameters"]["command"] = "unexpected executable content"
        with self.assertRaises(ContractError):
            self.run_guard(proposal=proposal)
        dependencies = copy.deepcopy(self.dependencies)
        del dependencies["target"]["uid"]
        with self.assertRaises(ContractError):
            self.run_guard(dependencies=dependencies)

    def test_test_clock_requires_timezone_aware_utc(self):
        for now in (NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=8)))):
            with self.subTest(now=now), self.assertRaises(GuardInputError):
                self.run_guard(now=now)

    def test_evaluation_leaves_inputs_unchanged_and_does_not_alias_target(self):
        inputs = (self.proposal, self.live_state, self.policy, self.baseline,
                  self.dependencies, self.approval())
        originals = copy.deepcopy(inputs)
        result = evaluate(*inputs, now=NOW)
        self.assertEqual(inputs, originals)
        result["target"]["name"] = "modified-output-target"
        self.assertEqual(inputs, originals)

    def test_canonical_fingerprints_ignore_key_order_but_include_evidence(self):
        reordered = dict(reversed(list(self.proposal.items())))
        reordered["target"] = dict(reversed(list(self.proposal["target"].items())))
        self.assertEqual(proposal_fingerprint(reordered), proposal_fingerprint(self.proposal))
        reordered["evidence_refs"] = self.proposal["evidence_refs"] + ["new-evidence"]
        self.assertNotEqual(proposal_fingerprint(reordered), proposal_fingerprint(self.proposal))
        self.assertEqual(template_sha256(self.baseline["pod_template"]), self.baseline["template_sha256"])


if __name__ == "__main__":
    unittest.main()
