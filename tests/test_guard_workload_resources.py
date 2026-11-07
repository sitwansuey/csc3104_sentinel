"""Workload budgets account for concurrent containers and rollout overlap."""

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guard.common import GuardInputError
from services.guard.resources import assess, validate_policy


NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def container(name="app", cpu="100m", memory="64Mi", *, sidecar=False):
    result = {"name": name, "image": "example/image@sha256:" + "a" * 64,
              "resources": {"requests": {"cpu": cpu, "memory": memory}}}
    if sidecar:
        result["restartPolicy"] = "Always"
    return result


class WorkloadResourceTests(unittest.TestCase):
    def setUp(self):
        self.proposal = {"action": "ROLLBACK"}
        self.baseline = {"pod_template": {"spec": {"containers": [container()]}}}
        self.live = {"deployment": {"spec": {"replicas": 3, "template": deepcopy(self.baseline["pod_template"])}, "status": {"replicas": 3}}}
        self.policy = {"workload_constraints": {
            "max_pod_requests": {"cpu": "100m", "memory": "64Mi"},
            "max_rollout_requests": {"cpu": "400m", "memory": "256Mi"},
            "max_replicas": 3, "max_surge_replicas": 1,
        }}

    @property
    def spec(self):
        return self.baseline["pod_template"]["spec"]

    def findings(self):
        result = assess(self.proposal, self.live, self.baseline, self.policy, NOW)
        json.dumps(result, allow_nan=False)
        return result

    def bad(self, code, status="FAIL"):
        findings = self.findings()
        self.assertTrue(any(item["code"] == code and item["status"] == status for item in findings), findings)
        return findings

    def safe(self):
        findings = self.findings()
        self.assertFalse(any(item["status"] in ("FAIL", "UNKNOWN") for item in findings), findings)
        return findings

    def pod_budget_only(self, cpu="100m", memory="64Mi"):
        self.policy = {"workload_constraints": {"max_pod_requests": {"cpu": cpu, "memory": memory}}}

    def test_exact_pod_and_default_surge_rollout_boundaries_pass(self):
        result = self.safe()
        cpu = next(item for item in result if item["path"].endswith("rollout_requests.cpu"))
        self.assertEqual(cpu["actual"], {"request_bound": "400m", "priced_replicas": 4, "per_pod_max": "100m"})

    def test_pod_above_budget_is_rejected(self):
        self.pod_budget_only()
        self.spec["containers"][0]["resources"]["requests"]["cpu"] = "101m"
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_rollout_above_budget_is_rejected(self):
        self.policy["workload_constraints"]["max_rollout_requests"]["cpu"] = "399m"
        self.bad("WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY")

    def test_regular_container_requests_sum(self):
        self.pod_budget_only()
        self.spec["containers"].append(container("other", "1m", "1"))
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_regular_init_peak_is_maximum_not_sum(self):
        self.pod_budget_only("250m", "128Mi")
        self.spec["initContainers"] = [container("init1", "250m", "128Mi"), container("init2", "200m", "100Mi")]
        self.safe()
        self.policy["workload_constraints"]["max_pod_requests"]["cpu"] = "249m"
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_restartable_init_sidecars_are_added_to_application_requests(self):
        self.pod_budget_only("300m", "192Mi")
        self.spec["initContainers"] = [container("sidecar1", sidecar=True), container("sidecar2", sidecar=True)]
        self.safe()
        self.policy["workload_constraints"]["max_pod_requests"]["cpu"] = "299m"
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_init_phase_includes_only_preceding_running_sidecars(self):
        self.pod_budget_only("500m", "192Mi")
        self.spec["initContainers"] = [container("early-sidecar", "200m", "64Mi", sidecar=True), container("initialization", "300m", "128Mi"), container("late-sidecar", "100m", "64Mi", sidecar=True)]
        self.safe()
        # Correct peak is early sidecar + initialization = 500m, not 400m
        # app+sidecars, and not 600m counting a sidecar that starts later.
        self.policy["workload_constraints"]["max_pod_requests"]["cpu"] = "499m"
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_pod_overhead_added_after_initialization_peak(self):
        self.pod_budget_only("110m", "65Mi")
        self.spec["overhead"] = {"cpu": "10m", "memory": "1Mi"}
        self.safe()
        self.policy["workload_constraints"]["max_pod_requests"]["memory"] = "64Mi"
        self.bad("WORKLOAD_POD_REQUEST_EXCEEDS_POLICY")

    def test_missing_request_uses_supplied_limit(self):
        self.spec["containers"][0]["resources"] = {"limits": {"cpu": "100m", "memory": "64Mi"}}
        self.safe()

    def test_missing_request_and_limit_is_unknown_not_zero(self):
        del self.spec["containers"][0]["resources"]["requests"]["cpu"]
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_missing_resource_for_nonbudgeted_dimension_does_not_block(self):
        self.policy = {"workload_constraints": {"max_pod_requests": {"cpu": "100m"}}}
        del self.spec["containers"][0]["resources"]["requests"]["memory"]
        self.safe()

    def test_pod_level_resources_are_explicitly_unsupported(self):
        self.spec["resources"] = {"requests": {"cpu": "1"}}
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_runtimeclass_without_trusted_overhead_is_unknown(self):
        self.spec["runtimeClassName"] = "gvisor"
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")
        self.spec["overhead"] = {}
        self.safe()

    def test_rollout_uses_larger_current_template_request(self):
        self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"] = "200m"
        findings = self.bad("WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY")
        cpu = next(item for item in findings if item["path"].endswith("rollout_requests.cpu"))
        self.assertEqual(cpu["actual"]["request_bound"], "800m")

    def test_rollout_requires_trusted_current_template(self):
        del self.live["deployment"]["spec"]["template"]
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_old_template_with_missing_resource_request_is_unknown(self):
        del self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["memory"]
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_old_template_pod_level_budget_is_unknown(self):
        self.live["deployment"]["spec"]["template"]["spec"]["resources"] = {}
        self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_percentage_surge_rounds_up(self):
        self.live["deployment"]["spec"]["replicas"] = 5
        self.live["deployment"]["spec"]["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "21%"}}
        self.policy["workload_constraints"] = {"max_replicas": 5, "max_surge_replicas": 2, "max_rollout_requests": {"cpu": "700m"}}
        self.safe()
        self.policy["workload_constraints"]["max_surge_replicas"] = 1
        self.bad("WORKLOAD_SURGE_EXCEEDS_POLICY")

    def test_absolute_surge_and_replicas_caps(self):
        self.live["deployment"]["spec"]["strategy"] = {"rollingUpdate": {"maxSurge": 2}}
        self.bad("WORKLOAD_SURGE_EXCEEDS_POLICY")
        self.policy["workload_constraints"]["max_replicas"] = 2
        self.bad("WORKLOAD_REPLICAS_EXCEED_POLICY")

    def test_recreate_has_zero_surge(self):
        self.live["deployment"]["spec"]["strategy"] = {"type": "Recreate"}
        self.policy["workload_constraints"]["max_surge_replicas"] = 0
        self.policy["workload_constraints"]["max_rollout_requests"]["cpu"] = "300m"
        self.safe()

    def test_status_replicas_over_desired_plus_surge_are_priced(self):
        self.live["deployment"]["status"]["replicas"] = 6
        findings = self.bad("WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY")
        cpu = next(item for item in findings if item["path"].endswith("rollout_requests.cpu"))
        self.assertEqual(cpu["actual"]["priced_replicas"], 6)

    def test_known_terminating_replicas_are_added(self):
        self.live["deployment"]["status"]["terminatingReplicas"] = 2
        findings = self.bad("WORKLOAD_ROLLOUT_REQUEST_EXCEEDS_POLICY")
        cpu = next(item for item in findings if item["path"].endswith("rollout_requests.cpu"))
        self.assertEqual(cpu["actual"]["priced_replicas"], 6)

    def test_scaled_to_zero_is_valid(self):
        self.live["deployment"]["spec"]["replicas"] = 0
        self.live["deployment"]["status"]["replicas"] = 0
        self.policy["workload_constraints"]["max_replicas"] = 0
        self.policy["workload_constraints"]["max_surge_replicas"] = 0
        self.policy["workload_constraints"]["max_rollout_requests"] = {"cpu": 0, "memory": 0}
        self.safe()

    def test_bad_or_missing_replicas_are_unknown(self):
        for value in (None, True, -1, "3", 1.5, 2 ** 31):
            with self.subTest(value=value):
                self.live["deployment"]["spec"]["replicas"] = value
                self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_malformed_or_unsupported_rollout_strategy_is_unknown(self):
        for value in (None, [], {"type": "Other"}, {"type": "Recreate", "rollingUpdate": {}}, {"rollingUpdate": None}, {"rollingUpdate": {"maxSurge": True}}, {"rollingUpdate": {"maxSurge": "-1%"}}, {"rollingUpdate": {"maxSurge": "00%", "maxUnavailable": 0}}, {"rollingUpdate": {"maxUnavailable": "101%"}}):
            with self.subTest(strategy=value):
                self.live["deployment"]["spec"]["strategy"] = value
                self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_invalid_status_counts_are_unknown(self):
        for key in ("replicas", "terminatingReplicas"):
            for value in (None, True, -1, "1"):
                with self.subTest(key=key, value=value):
                    self.live["deployment"]["status"] = {key: value}
                    self.bad("WORKLOAD_RESOURCE_UNKNOWN", "UNKNOWN")

    def test_invalid_resource_shapes_and_quantities_fail(self):
        for value in (True, "-1m", "bad", "0.1m"):
            with self.subTest(value=value):
                self.spec["containers"][0]["resources"]["requests"]["cpu"] = value
                self.bad("INVALID_RESOURCE_QUANTITY")
        self.spec["containers"][0]["resources"] = []
        self.bad("INVALID_RESOURCE_TEMPLATE")

    def test_bad_init_restart_policy_fails(self):
        self.spec["initContainers"] = [container("init")]
        self.spec["initContainers"][0]["restartPolicy"] = "OnFailure"
        self.bad("INVALID_RESOURCE_TEMPLATE")

    def test_bad_overhead_fails(self):
        self.spec["overhead"] = {"memory": "bad"}
        self.bad("INVALID_RESOURCE_QUANTITY")

    def test_strict_workload_policy_rejects_bad_shapes_and_quantities(self):
        for value in (None, [], {"max_pod_request": {}}, {"max_pod_requests": None}, {"max_pod_requests": {"gpu": 1}}, {"max_rollout_requests": {"cpu": "bad"}}, {"max_replicas": True}, {"max_replicas": -1}, {"max_surge_replicas": "1"}):
            with self.subTest(policy=value):
                with self.assertRaises(GuardInputError):
                    validate_policy({"workload_constraints": value})

    def test_absent_blocks_skip_and_empty_blocks_pass(self):
        self.policy = {}
        self.assertEqual(self.findings()[0]["status"], "SKIP")
        self.policy = {"workload_constraints": {}}
        self.safe()

    def test_assessment_never_mutates_inputs(self):
        before = deepcopy((self.proposal, self.live, self.baseline, self.policy))
        self.safe()
        self.assertEqual((self.proposal, self.live, self.baseline, self.policy), before)

    def test_per_container_failure_explains_exact_container_and_ceiling(self):
        self.policy["resource_constraints"] = {"max_container_limits": {"memory": "128Mi"}}
        self.spec["containers"][0]["resources"]["limits"] = {"memory": "256Mi"}
        findings = self.bad("RESOURCE_LIMIT_EXCEEDS_POLICY")
        item = next(item for item in findings if item["code"] == "RESOURCE_LIMIT_EXCEEDS_POLICY")
        self.assertEqual(item["path"], "baseline.pod_template.spec.containers[0].resources.limits.memory")
        self.assertIn("'app'", item["message"])
        self.assertEqual(item["actual"], "256Mi")
        self.assertEqual(item["expected"], "128Mi")

    def test_malformed_resource_structures_do_not_echo_private_values(self):
        marker = "PRIVATE secret environment contents must not enter the report"
        mutations = (
            lambda: self.spec.update(containers={"env": marker}),
            lambda: self.spec.update(containers=[marker]),
            lambda: self.spec["containers"][0].update(resources=[{"env": marker}]),
            lambda: self.spec["containers"][0]["resources"].update(requests=[{"env": marker}]),
            lambda: self.spec["containers"][0]["resources"]["requests"].update(cpu={"env": marker}),
            lambda: self.spec["containers"][0]["resources"]["requests"].update(cpu=marker),
            lambda: self.spec.update(overhead=[{"env": marker}]),
            lambda: self.spec.update(overhead={"cpu": {"env": marker}}),
            lambda: self.spec.update(resources={"env": marker}),
            lambda: self.live["deployment"]["spec"].update(template=[{"env": marker}]),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                self.setUp()
                mutate()
                findings = self.findings()
                self.assertTrue(any(item["status"] in ("FAIL", "UNKNOWN") for item in findings))
                self.assertNotIn(marker, json.dumps(findings))

    def test_invalid_quantity_findings_report_type_without_echoing_value(self):
        self.spec["containers"][0]["resources"]["requests"]["cpu"] = "private-value"
        findings = self.bad("INVALID_RESOURCE_QUANTITY")
        item = next(item for item in findings if item["code"] == "INVALID_RESOURCE_QUANTITY")
        self.assertEqual(item["actual"], "invalid str quantity")
        self.assertNotIn("private-value", json.dumps(findings))

    def test_invalid_or_overlong_container_names_are_sanitized(self):
        for name in ({"private": "PRIVATE VALUE"}, "PRIVATE VALUE", "a" * 64):
            with self.subTest(name_type=type(name).__name__):
                self.setUp()
                self.spec["containers"][0]["name"] = name
                self.spec["containers"][0]["resources"]["requests"]["cpu"] = "invalid"
                findings = self.bad("INVALID_RESOURCE_QUANTITY")
                rendered = json.dumps(findings)
                self.assertNotIn("PRIVATE VALUE", rendered)
                self.assertNotIn("a" * 64, rendered)
                self.assertIn("invalid name", rendered)

    def test_unsupported_init_restart_policy_does_not_echo_private_values(self):
        marker = "PRIVATE secret in malformed restart policy"
        self.spec["initContainers"] = [container("init")]
        self.spec["initContainers"][0]["restartPolicy"] = {"env": marker}
        findings = self.bad("INVALID_RESOURCE_TEMPLATE")
        self.assertNotIn(marker, json.dumps(findings))


if __name__ == "__main__":
    unittest.main()
