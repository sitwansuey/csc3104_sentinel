"""Reject redundant recovery proposals even when operator approval is valid."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from runtime.executor_pipeline import GuardAuthorizer
from services.executor.core import ExecutionAdapters, execute
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger
from services.guard.core import (
    GuardInputError, assert_decision_usable, evaluate_with_report,
    proposal_fingerprint, template_sha256,
)
from tests.support.executor_fake import FakeDeploymentAPI


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=timezone.utc)


def fixture(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


class GuardNecessityTests(unittest.TestCase):
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
            "operator_id": "trusted-human-reviewer", "decision": "APPROVED",
            "approved_at": "2026-10-10T12:00:35Z",
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
        }

    def args(self):
        return (self.proposal, self.live, self.policy, self.baseline,
                self.dependencies, self.approval)

    def decide(self):
        return evaluate_with_report(*self.args(), now=NOW)

    def same_template(self):
        self.live["deployment"]["spec"]["template"] = deepcopy(self.baseline["pod_template"])

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
        digest = template_sha256(self.baseline["pod_template"])
        self.baseline["template_sha256"] = digest
        self.proposal["parameters"]["template_sha256"] = digest
        self.baseline["reference_versions"] = []
        self.live["reference_versions"] = []
        self.baseline["health_evidence"] = {
            "source": "sentinel-verifier", "evidence_id": "healthy-window-1", "status": "HEALTHY",
            "target": deepcopy(self.proposal["target"]), "template_sha256": digest,
            "window_start": "2026-10-10T11:58:30Z",
            "window_end": self.baseline["healthy_observed_at"],
        }
        self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)
        self.same_template()
        self.live["deployment"]["spec"]["template"]["spec"]["containers"][0]["env"][0]["value"] = "broken"

    def test_healthy_approved_identical_template_is_rejected(self):
        self.make_live_profile()
        self.same_template()
        self.live["deployment"]["status"].update(readyReplicas=1, availableReplicas=1)
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertEqual(decision["reason_codes"], ["ROLLBACK_ALREADY_AT_BASELINE"])
        check = next(item for item in report["checks"] if item["code"] == "ROLLBACK_ALREADY_AT_BASELINE")
        self.assertEqual(check["status"], "FAIL")
        self.assertEqual(check["actual"], {"template_sha256": self.baseline["template_sha256"]})
        self.assertIn("no template change", check["message"])

    def test_unavailable_replicas_do_not_justify_identical_rollback(self):
        self.same_template()
        self.live["deployment"]["status"].update(readyReplicas=0, availableReplicas=0)
        decision, _ = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertEqual(decision["reason_codes"], ["ROLLBACK_ALREADY_AT_BASELINE"])

    def test_different_live_template_remains_allowed(self):
        self.make_live_profile()
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "ALLOW", decision)
        check = next(item for item in report["checks"] if item["code"] == "ROLLBACK_CHANGES_TEMPLATE")
        self.assertEqual(check["status"], "PASS")

    def test_canonical_comparison_ignores_mapping_order(self):
        self.same_template()
        original = self.live["deployment"]["spec"]["template"]
        self.live["deployment"]["spec"]["template"] = dict(reversed(list(original.items())))
        decision, _ = self.decide()
        self.assertEqual(decision["reason_codes"], ["ROLLBACK_ALREADY_AT_BASELINE"])

    def test_development_fixture_without_template_remains_compatible(self):
        del self.live["deployment"]["spec"]["template"]
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "ALLOW", decision)
        self.assertTrue(any(item["code"] == "ROLLBACK_CHANGE_UNKNOWN" and item["status"] == "SKIP"
                            for item in report["checks"]))

    def test_live_missing_or_malformed_template_fails_closed(self):
        for template in (None, [], {}, {"spec": {}}, {"spec": {"containers": []}},
                         {"spec": {"containers": ["invalid"]}}):
            with self.subTest(template=template):
                self.setUp()
                self.make_live_profile()
                if template is None:
                    del self.live["deployment"]["spec"]["template"]
                else:
                    self.live["deployment"]["spec"]["template"] = template
                decision, report = self.decide()
                self.assertIn(decision["verdict"], ("ESCALATE", "REJECT"), decision)
                if template is None:
                    self.assertEqual(decision["verdict"], "ESCALATE")
                    self.assertIn("ROLLBACK_CHANGE_UNKNOWN", decision["reason_codes"])
                self.assertTrue(any(item["code"] == "ROLLBACK_CHANGE_UNKNOWN" and item["status"] == "UNKNOWN"
                                    for item in report["checks"]))

    def test_invalid_baseline_is_not_misreported_as_unnecessary_recovery(self):
        for baseline_field, value in (("template_sha256", "b" * 64), ("baseline_id", "another-baseline"),
                                      ("target", dict(self.proposal["target"], uid="another-uid"))):
            with self.subTest(baseline_field=baseline_field):
                self.setUp()
                self.same_template()
                self.baseline[baseline_field] = value
                decision, report = self.decide()
                self.assertEqual(decision["verdict"], "REJECT")
                self.assertNotIn("ROLLBACK_ALREADY_AT_BASELINE", decision["reason_codes"])
                self.assertFalse(any(item["code"] in {"ROLLBACK_ALREADY_AT_BASELINE", "ROLLBACK_CHANGES_TEMPLATE",
                                                     "ROLLBACK_CHANGE_UNKNOWN"} for item in report["checks"]))

    def test_nonrollback_action_does_not_run_template_comparison(self):
        self.same_template()
        self.proposal.update(action="SCALE", parameters={"desired_replicas": 2})
        self.approval["proposal_sha256"] = proposal_fingerprint(self.proposal)
        decision, report = self.decide()
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertNotIn("ROLLBACK_ALREADY_AT_BASELINE", decision["reason_codes"])
        self.assertFalse(any(item["code"] in {"ROLLBACK_ALREADY_AT_BASELINE", "ROLLBACK_CHANGES_TEMPLATE",
                                             "ROLLBACK_CHANGE_UNKNOWN"} for item in report["checks"]))

    def test_rejection_does_not_mutate_inputs_or_expose_template_contents(self):
        self.same_template()
        before = deepcopy(self.args())
        decision, report = self.decide()
        self.assertEqual(self.args(), before)
        check = next(item for item in report["checks"] if item["code"] == "ROLLBACK_ALREADY_AT_BASELINE")
        self.assertNotIn("containers", json.dumps(check))
        with self.assertRaises(GuardInputError):
            assert_decision_usable(decision, *self.args(), now=NOW)

    def test_fresh_executor_validation_refuses_redundant_approved_write(self):
        self.make_live_profile()
        self.same_template()
        cluster = FakeDeploymentAPI(self.live, clock=lambda: NOW)
        authorizer = GuardAuthorizer(
            policy_reader=lambda proposal: self.policy,
            baseline_reader=lambda proposal: self.baseline,
            dependency_reader=lambda proposal: self.dependencies,
            approval_reader=lambda proposal: self.approval,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with SQLiteExecutionLedger(root / "executor.sqlite") as ledger:
                adapters = ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(root / "locks"),
                                             cluster=cluster, authorizer=authorizer, clock=lambda: NOW)
                result = execute(self.proposal, adapters)
                self.assertEqual(result["status"], "FAILED")
                self.assertIn("ROLLBACK_ALREADY_AT_BASELINE", result["details"])
                self.assertEqual(cluster.write_attempts, [])
                self.assertEqual(cluster.writes, [])
                self.assertEqual(cluster.snapshot, self.live)
                row = ledger.get(self.proposal["action_id"])
                self.assertEqual(row["attempts"], 0)
                self.assertEqual(row["guard_result"]["verdict"], "REJECT")


class ChaosNegativeMatrixTests(unittest.TestCase):
    """The P4.03 2x2 matrix plus the CPU-stress cell, through the real Guard and the Executor's fresh check.

    Faults: a pod failure (Pods not ready, template at the baseline), a config
    drift (the R1 template fault) and CPU stress (every Pod ready, template at
    the baseline). Actions: ROLLBACK and RESTART. The approval is valid in
    every cell, so only the Guard's necessity checks can refuse.
    """

    def setUp(self):
        from tests.test_restart_action import case
        self.inputs = case()  # pod failure, RESTART allowed under a one-per-hour budget, empty history
        self.observations = fixture("data/sentinel/samples/observations.json")

    def state(self, fault):
        live = {"pod": self.inputs["live_state"], "drift": self.observations["failed-rollout"][-1],
                "cpu": self.observations["healthy"][-1]}[fault]
        live = deepcopy(live)
        live["restart_history"] = []
        if fault == "cpu":
            # Stress inside the Pod leaves it ready and the template at the baseline.
            live["deployment"]["spec"]["template"] = deepcopy(self.inputs["baseline"]["pod_template"])
        return live

    def proposal(self, action, live):
        from tests.test_restart_action import bind, restart_proposal
        if action == "RESTART":
            return restart_proposal(self.inputs["baseline"], live)
        baseline = self.inputs["baseline"]
        return bind({**self.inputs["proposal"], "action": "ROLLBACK",
                     "based_on_resource_version": live["deployment"]["metadata"]["resourceVersion"],
                     "parameters": {key: baseline[key] for key in ("baseline_id", "template_sha256")}})

    def decide(self, fault, action):
        from tests.test_restart_action import approval_for, with_proposal
        live = self.state(fault)
        inputs = with_proposal(self.inputs, self.proposal(action, live)) | {"live_state": live}
        approval = approval_for(inputs["proposal"], inputs["policy"])
        return evaluate_with_report(**inputs, approval=approval, now=NOW)[0], inputs, approval

    def test_each_cell_gets_the_pre_registered_verdict(self):
        cells = {("pod", "RESTART"): ("ALLOW", "VALIDATION_PASSED"),
                 ("pod", "ROLLBACK"): ("REJECT", "ROLLBACK_ALREADY_AT_BASELINE"),
                 ("drift", "ROLLBACK"): ("ALLOW", "VALIDATION_PASSED"),
                 ("drift", "RESTART"): ("REJECT", "RESTART_TEMPLATE_DRIFT"),
                 ("cpu", "RESTART"): ("REJECT", "RESTART_NOT_JUSTIFIED"),
                 ("cpu", "ROLLBACK"): ("REJECT", "ROLLBACK_ALREADY_AT_BASELINE")}
        for (fault, action), (verdict, code) in cells.items():
            with self.subTest(fault=fault, action=action):
                decision, _, _ = self.decide(fault, action)
                self.assertEqual(decision["verdict"], verdict, decision["reason_codes"])
                self.assertIn(code, decision["reason_codes"])

    def test_the_harness_expected_codes_match_the_guard(self):
        from experiments.boutique.harness.chaos_recovery import FORCED
        for (fault, action), code in FORCED.items():
            with self.subTest(fault=fault, action=action):
                decision, _, _ = self.decide(fault, action)
                self.assertEqual(decision["verdict"], "REJECT")
                self.assertIn(code, decision["reason_codes"])

    def test_restart_during_cpu_stress_is_not_justified_but_is_with_unavailable_replicas(self):
        decision, _, _ = self.decide("cpu", "RESTART")
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("RESTART_NOT_JUSTIFIED", decision["reason_codes"])
        self.assertNotIn("RESTART_TEMPLATE_DRIFT", decision["reason_codes"])
        decision, _, _ = self.decide("pod", "RESTART")
        self.assertNotIn("RESTART_NOT_JUSTIFIED", decision["reason_codes"])

    def test_forced_wrong_actions_make_zero_executor_writes(self):
        for fault, action, code in (("pod", "ROLLBACK", "ROLLBACK_ALREADY_AT_BASELINE"),
                                    ("drift", "RESTART", "RESTART_TEMPLATE_DRIFT"),
                                    ("cpu", "RESTART", "RESTART_NOT_JUSTIFIED")):
            with self.subTest(fault=fault, action=action):
                _, inputs, approval = self.decide(fault, action)
                cluster = FakeDeploymentAPI(inputs["live_state"], clock=lambda: NOW)
                authorizer = GuardAuthorizer(policy_reader=lambda proposal, inputs=inputs: inputs["policy"],
                                             baseline_reader=lambda proposal, inputs=inputs: inputs["baseline"],
                                             dependency_reader=lambda proposal, inputs=inputs: inputs["dependencies"],
                                             approval_reader=lambda proposal, approval=approval: approval)
                with tempfile.TemporaryDirectory() as directory, \
                        SQLiteExecutionLedger(Path(directory) / "executor.sqlite") as ledger:
                    adapters = ExecutionAdapters(ledger=ledger, coordination=LocalTargetCoordinator(Path(directory) / "locks"),
                                                 cluster=cluster, authorizer=authorizer, clock=lambda: NOW)
                    result = execute(inputs["proposal"], adapters)
                    row = ledger.get(inputs["proposal"]["action_id"])
                self.assertEqual(result["status"], "FAILED")
                self.assertIn(code, row["guard_result"]["reason_codes"])
                self.assertEqual((cluster.write_attempts, cluster.writes, row["attempts"]), ([], [], 0))


if __name__ == "__main__":
    unittest.main()
