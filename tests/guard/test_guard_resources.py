"""Rollback resource ceilings are enforced before operator approval."""

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from services.guard.core import GuardInputError, evaluate, proposal_fingerprint, template_sha256
from shared.contracts import validate_message


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class GuardResourceTests(unittest.TestCase):
    def setUp(self):
        self.proposal = fixture("data/sentinel/samples/messages/proposal.json")
        self.live = fixture("data/sentinel/samples/observations.json")["failed-rollout"][-1]
        self.policy = fixture("data/sentinel/samples/policy.json")
        self.baseline = fixture("data/sentinel/samples/baseline.json")
        self.dependencies = fixture("data/sentinel/samples/messages/dependency-result.json")
        self.policy["resource_constraints"] = {
            "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
            "max_container_limits": {"cpu": "500m", "memory": "128Mi"},
            "require_requests_and_limits": True,
        }

    @property
    def resources(self):
        return self.baseline["pod_template"]["spec"]["containers"][0]["resources"]

    def check(self):
        # Each test deliberately reviews and approves the actual candidate.
        # Resource violations must still reject a valid hash and approval.
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        approval = {
            "action_id": self.proposal["action_id"],
            "proposal_sha256": proposal_fingerprint(self.proposal),
            "policy_version": self.policy["policy_version"],
            "operator_id": "resource-policy-reviewer",
            "decision": "APPROVED",
            "approved_at": "2026-10-10T12:00:35Z",
        }
        result = evaluate(self.proposal, self.live, self.policy, self.baseline,
                          self.dependencies, approval, now=NOW)
        validate_message(result)
        return result

    def allows(self):
        self.assertEqual(self.check()["verdict"], "ALLOW")

    def rejects(self, reason):
        result = self.check()
        self.assertEqual(result["verdict"], "REJECT")
        self.assertIn(reason, result["reason_codes"])
        self.assertNotIn("OPERATOR_APPROVAL_REQUIRED", result["reason_codes"])

    def test_current_healthy_baseline_satisfies_resource_policy(self):
        self.allows()

    def test_equal_request_limit_and_policy_boundaries_allow(self):
        self.resources["requests"] = {"cpu": "0.1", "memory": "64Mi"}
        self.resources["limits"] = {"cpu": "100m", "memory": 67108864}
        self.policy["resource_constraints"]["max_container_limits"] = {
            "cpu": ".100", "memory": "67108864",
        }
        self.allows()

    def test_cpu_cores_milli_and_exponent_are_equivalent(self):
        for value in ("100m", ".1", "0.1000", "1e-1", "1E-1", 0.1):
            with self.subTest(value=value):
                self.resources["requests"]["cpu"] = value
                self.allows()

    def test_decimal_binary_exponent_and_numeric_memory_units_are_exact(self):
        equivalents = ("1Mi", "1024Ki", "1048576", 1048576, 1048576.0,
                       "1.048576M", "1048.576k", "1048576000m", "1048576e0")
        self.policy["resource_constraints"]["max_container_requests"]["memory"] = "1Mi"
        for value in equivalents:
            with self.subTest(value=value):
                self.resources["requests"]["memory"] = value
                self.allows()

    def test_all_documented_large_si_and_binary_memory_suffixes(self):
        for prefix, exponent in (("k", 1), ("M", 2), ("G", 3), ("T", 4), ("P", 5), ("E", 6)):
            with self.subTest(prefix=prefix):
                self.resources["requests"]["memory"] = f"1{prefix}"
                self.resources["limits"]["memory"] = f"1{prefix}"
                self.policy["resource_constraints"]["max_container_requests"]["memory"] = str(1000 ** exponent)
                self.policy["resource_constraints"]["max_container_limits"]["memory"] = str(1000 ** exponent)
                self.allows()
        for exponent, prefix in enumerate("KMGTPE", 1):
            with self.subTest(prefix=prefix + "i"):
                self.resources["requests"]["memory"] = f"1{prefix}i"
                self.resources["limits"]["memory"] = f"1{prefix}i"
                self.policy["resource_constraints"]["max_container_requests"]["memory"] = str(1024 ** exponent)
                self.policy["resource_constraints"]["max_container_limits"]["memory"] = str(1024 ** exponent)
                self.allows()

    def test_decimal_megabytes_do_not_equal_mebibytes(self):
        self.resources["limits"]["memory"] = "128Mi"
        self.policy["resource_constraints"]["max_container_limits"]["memory"] = "128M"
        self.rejects("RESOURCE_LIMIT_EXCEEDS_POLICY")

    def test_each_request_and_limit_ceiling_rejects_above_boundary(self):
        for group, resource, value, reason in (
            ("requests", "cpu", "101m", "RESOURCE_REQUEST_EXCEEDS_POLICY"),
            ("requests", "memory", "67108865", "RESOURCE_REQUEST_EXCEEDS_POLICY"),
            ("limits", "cpu", "501m", "RESOURCE_LIMIT_EXCEEDS_POLICY"),
            ("limits", "memory", "134217729", "RESOURCE_LIMIT_EXCEEDS_POLICY"),
        ):
            with self.subTest(group=group, resource=resource):
                original = copy.deepcopy(self.resources)
                self.resources[group][resource] = value
                self.rejects(reason)
                self.baseline["pod_template"]["spec"]["containers"][0]["resources"] = original

    def test_requests_cannot_exceed_limits_even_below_policy_ceiling(self):
        for resource, value in (("cpu", "49m"), ("memory", "31Mi")):
            with self.subTest(resource=resource):
                original = self.resources["limits"][resource]
                self.resources["limits"][resource] = value
                self.rejects("RESOURCE_REQUEST_EXCEEDS_LIMIT")
                self.resources["limits"][resource] = original

    def test_explicit_flag_requires_both_resources_in_both_groups(self):
        for group in ("requests", "limits"):
            for resource in ("cpu", "memory"):
                with self.subTest(group=group, resource=resource):
                    value = self.resources[group].pop(resource)
                    self.rejects("RESOURCE_VALUES_REQUIRED")
                    self.resources[group][resource] = value

    def test_missing_resource_block_is_rejected_when_required(self):
        del self.baseline["pod_template"]["spec"]["containers"][0]["resources"]
        self.rejects("RESOURCE_VALUES_REQUIRED")

    def test_missing_resources_allow_when_explicit_values_are_optional(self):
        self.policy["resource_constraints"] = {"require_requests_and_limits": False}
        del self.baseline["pod_template"]["spec"]["containers"][0]["resources"]
        self.allows()

    def test_require_flag_defaults_false(self):
        self.policy["resource_constraints"] = {}
        del self.baseline["pod_template"]["spec"]["containers"][0]["resources"]
        self.allows()

    def test_omitted_request_uses_its_limit_for_request_ceiling(self):
        self.policy["resource_constraints"]["require_requests_and_limits"] = False
        del self.resources["requests"]["cpu"]
        self.resources["limits"]["cpu"] = "200m"
        self.rejects("RESOURCE_REQUEST_EXCEEDS_POLICY")

    def test_missing_limit_can_be_optional_but_supplied_request_is_still_checked(self):
        self.policy["resource_constraints"]["require_requests_and_limits"] = False
        del self.policy["resource_constraints"]["max_container_limits"]["cpu"]
        del self.resources["limits"]["cpu"]
        self.allows()
        self.resources["requests"]["cpu"] = "101m"
        self.rejects("RESOURCE_REQUEST_EXCEEDS_POLICY")

    def test_capped_limits_cannot_be_omitted_when_explicit_values_are_optional(self):
        self.policy["resource_constraints"]["require_requests_and_limits"] = False
        for resource in ("cpu", "memory"):
            with self.subTest(resource=resource):
                value = self.resources["limits"].pop(resource)
                self.rejects("RESOURCE_VALUES_REQUIRED")
                self.resources["limits"][resource] = value

    def test_capped_requests_need_explicit_request_or_effective_limit(self):
        self.policy["resource_constraints"] = {
            "max_container_requests": {"cpu": "100m", "memory": "64Mi"},
            "require_requests_and_limits": False,
        }
        for resource in ("cpu", "memory"):
            with self.subTest(resource=resource):
                saved_request = self.resources["requests"].pop(resource)
                saved_limit = self.resources["limits"].pop(resource)
                self.rejects("RESOURCE_VALUES_REQUIRED")
                self.resources["limits"][resource] = saved_request
                self.allows()
                self.resources["requests"][resource] = saved_request
                self.resources["limits"][resource] = saved_limit

    def test_cpu_precision_finer_than_millicpu_is_not_rounded(self):
        for value in ("0.5m", "0.0001", "1e-4", 0.0001):
            with self.subTest(value=value):
                self.resources["requests"]["cpu"] = value
                self.rejects("INVALID_RESOURCE_QUANTITY")

    def test_fractional_memory_bytes_are_not_rounded(self):
        for value in ("400m", ".1", "1.1Ki", "1e-1", 0.5):
            with self.subTest(value=value):
                self.resources["requests"]["memory"] = value
                self.rejects("INVALID_RESOURCE_QUANTITY")

    def test_candidate_quantities_reject_malformed_negative_boolean_and_null(self):
        for resource in ("cpu", "memory"):
            for value in (None, True, False, -1, "-1", "NaN", "Infinity", "1e999999999",
                          "1e-999999999", "", " 1", "1 ", "1MB", "500mi", [], {}):
                with self.subTest(resource=resource, value=value):
                    self.resources["requests"][resource] = value
                    self.rejects("INVALID_RESOURCE_QUANTITY")
            self.resources["requests"][resource] = "50m" if resource == "cpu" else "32Mi"

    def test_kubernetes_quantity_magnitude_is_not_silently_capped(self):
        self.resources["requests"]["memory"] = str(2 ** 63)
        self.rejects("INVALID_RESOURCE_QUANTITY")

    def test_quantity_tokens_accept_256_characters_and_reject_longer_inputs(self):
        for resource, prefix in (("cpu", "0.1"), ("memory", "1.")):
            with self.subTest(resource=resource):
                value = prefix + "0" * (256 - len(prefix))
                self.resources["requests"][resource] = value
                self.allows()
                self.resources["requests"][resource] = value + "0"
                self.rejects("INVALID_RESOURCE_QUANTITY")
                self.resources["requests"][resource] = "50m" if resource == "cpu" else "32Mi"

    def test_overlong_quantity_tokens_and_huge_integer_policy_values_raise(self):
        for value in ("0.1" + "0" * 254, "0e" + "0" * 255, 10 ** 10000):
            # Do not render the huge integer in subtest diagnostics: conversion
            # itself exceeds Python's decimal-digit safety limit.
            with self.subTest(value_type=type(value).__name__):
                self.policy["resource_constraints"] = {"max_container_requests": {"cpu": value}}
                with self.assertRaises(GuardInputError):
                    self.check()

    def test_every_regular_container_is_checked(self):
        second = copy.deepcopy(self.baseline["pod_template"]["spec"]["containers"][0])
        second["name"] = "second-container"
        self.baseline["pod_template"]["spec"]["containers"].append(second)
        self.allows()
        second["resources"]["limits"]["memory"] = "129Mi"
        self.rejects("RESOURCE_LIMIT_EXCEEDS_POLICY")

    def test_init_containers_have_the_same_resource_constraints(self):
        init = copy.deepcopy(self.baseline["pod_template"]["spec"]["containers"][0])
        init["name"] = "initialize"
        self.baseline["pod_template"]["spec"]["initContainers"] = [init]
        self.allows()
        init["resources"]["requests"]["cpu"] = "101m"
        self.rejects("RESOURCE_REQUEST_EXCEEDS_POLICY")
        del init["resources"]["requests"]["cpu"]
        self.rejects("RESOURCE_VALUES_REQUIRED")

    def test_malformed_container_resource_structures_reject(self):
        container = self.baseline["pod_template"]["spec"]["containers"][0]
        for value in (None, [], "cpu=100m"):
            with self.subTest(value=value):
                container["resources"] = value
                self.rejects("INVALID_RESOURCE_TEMPLATE")
        for group in ("requests", "limits"):
            for value in (None, [], "100m"):
                with self.subTest(group=group, value=value):
                    container["resources"] = {"requests": {}, "limits": {}}
                    container["resources"][group] = value
                    self.rejects("INVALID_RESOURCE_TEMPLATE")

    def test_malformed_init_container_structure_rejects(self):
        for value in (None, {}, [None], ["init"]):
            with self.subTest(value=value):
                self.baseline["pod_template"]["spec"]["initContainers"] = value
                self.rejects("INVALID_RESOURCE_TEMPLATE")

    def test_other_kubernetes_resource_types_do_not_interfere_with_cpu_memory_scope(self):
        self.resources["requests"]["ephemeral-storage"] = "1Gi"
        self.resources["limits"]["nvidia.com/gpu"] = "1"
        self.allows()

    def test_absent_constraints_preserve_existing_policy_behavior(self):
        del self.policy["resource_constraints"]
        self.resources["limits"]["cpu"] = "5000m"
        self.allows()

    def test_zero_is_an_exact_valid_boundary(self):
        self.resources["requests"] = {"cpu": 0, "memory": "0"}
        self.resources["limits"] = {"cpu": "0m", "memory": "0Mi"}
        self.policy["resource_constraints"]["max_container_requests"] = {"cpu": 0, "memory": 0}
        self.policy["resource_constraints"]["max_container_limits"] = {"cpu": 0, "memory": 0}
        self.allows()

    def test_reason_codes_are_not_duplicated_across_containers(self):
        self.resources["limits"]["cpu"] = "501m"
        self.baseline["pod_template"]["spec"]["containers"].append(
            copy.deepcopy(self.baseline["pod_template"]["spec"]["containers"][0])
        )
        result = self.check()
        self.assertEqual(result["reason_codes"].count("RESOURCE_LIMIT_EXCEEDS_POLICY"), 1)

    def test_bad_resource_policy_shapes_and_unknown_keys_raise_input_error(self):
        invalid = (
            None, [], "limits", {"require_requests_and_limits": "true"},
            {"require_requests_and_limits": 1}, {"max_container_request": {"cpu": "1"}},
            {"max_container_limits": None}, {"max_container_requests": []},
            {"max_container_requests": {"CPU": "1"}},
            {"max_container_limits": {"ephemeral-storage": "1Gi"}},
        )
        for value in invalid:
            with self.subTest(value=value):
                self.policy["resource_constraints"] = value
                with self.assertRaises(GuardInputError):
                    self.check()

    def test_bad_policy_quantities_raise_instead_of_producing_guard_decision(self):
        for resource in ("cpu", "memory"):
            for value in (True, None, -1, "-1", "NaN", "Infinity", float("inf"), float("nan"),
                          "bad", {}, "1e999999999", "1e-999999999"):
                with self.subTest(resource=resource, value=value):
                    self.policy["resource_constraints"] = {"max_container_limits": {resource: value}}
                    with self.assertRaises(GuardInputError):
                        self.check()
        for resource, value in (("cpu", "0.1m"), ("memory", "400m"), ("memory", str(2 ** 63))):
            with self.subTest(resource=resource, value=value):
                self.policy["resource_constraints"] = {"max_container_limits": {resource: value}}
                with self.assertRaises(GuardInputError):
                    self.check()

    def test_resource_policy_evaluation_does_not_mutate_inputs(self):
        # Canonical fixture hashes are already current before evaluation.
        original = copy.deepcopy((self.policy, self.baseline))
        self.allows()
        self.assertEqual((self.policy, self.baseline), original)


if __name__ == "__main__":
    unittest.main()
