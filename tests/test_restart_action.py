"""RESTART as a typed action, end to end and offline (P3.08).

Real Planner, Coordinator, Guard, Executor and Verifier code with a fake
Deployment API. A RESTART writes the reviewed baseline template plus a
planner-bound token through the same conditional write as ROLLBACK. Nothing here
touches a cluster.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from experiments.validation.demo_offline_flow import OfflineFlow, demonstrate_restart, fixture, restart_policy
from runtime.executor_pipeline import GuardAuthorizer
from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import CoordinatorError, LocalEventBus, SQLiteStore, handle
from services.executor.actions import candidate_template, evidence as action_evidence
from services.executor.core import ACTION_ANNOTATION, ConditionalWriteConflict, ExecutionAdapters, execute, reconcile
from services.executor.reconciliation import Basis, action_attempt, evidence_fields_valid
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger, canonical_json, target_key
from services.guard import restart as guard_restart
from services.guard.core import GuardInputError, evaluate_with_report, proposal_fingerprint
from services.guard.decisions import validate_policy
from services.planner.core import mock_provider, propose, retrieve_runbooks
from services.observer.core import detect
from shared.actions import (
    RESTART_ANNOTATION, RESTART_HISTORY_LIMIT, config_fingerprint, current_restart_token, restart_parameters, restart_template,
    restart_token,
)
from shared.canonical import sha256_hex
from shared.contracts import ContractError, validate_message
from tests.support import executor_model as model
from tests.support.executor_fake import FakeDeploymentAPI

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
INCIDENT = "INC-RESTART-001"


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def bind(message):
    message = {key: value for key, value in message.items() if key != "action_id"}
    message["action_id"] = "ACT-" + sha256_hex(message)[:24]
    return message


def restart_proposal(baseline, live_state, incident_id=INCIDENT, **parameters):
    message = {"schema_version": "1.0", "kind": "proposal", "incident_id": incident_id,
               "target": deepcopy(baseline["target"]), "action": "RESTART",
               "parameters": {**restart_parameters(incident_id, baseline), **parameters},
               "based_on_resource_version": live_state["deployment"]["metadata"]["resourceVersion"],
               "rationale": "Pods are not ready while the configuration equals the reviewed baseline.",
               "evidence_refs": ["E-POD-RESTART"]}
    return bind(message)


def case(incident_id=INCIDENT):
    """Guard inputs for a RESTART of the sample target whose Pods are not ready."""
    baseline = fixture("baseline.json")
    live = fixture("observations.json")["pod-failure"][-1]
    live["restart_history"] = []
    proposal = restart_proposal(baseline, live, incident_id)
    dependencies = fixture("messages/dependency-result.json")
    dependencies.update(incident_id=incident_id, action_id=proposal["action_id"])
    return {"proposal": proposal, "live_state": live, "policy": restart_policy(fixture("policy.json")),
            "baseline": baseline, "dependencies": dependencies}


def approval_for(proposal, policy, **changes):
    record = {"action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
              "policy_version": policy["policy_version"], "operator_id": "fixture-operator",
              "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z", "expires_at": "2026-10-10T12:01:35Z"}
    record.update(changes)
    return record


def with_proposal(inputs, proposal):
    inputs = deepcopy(inputs)
    inputs["proposal"] = proposal
    inputs["dependencies"]["action_id"] = proposal["action_id"]
    inputs["dependencies"]["incident_id"] = proposal["incident_id"]
    return inputs


class RestartContractTests(unittest.TestCase):
    def setUp(self):
        self.proposal = case()["proposal"]

    def test_bound_restart_parameters_validate(self):
        validate_message(self.proposal)
        validate_message(fixture("messages/restart-proposal.json"))

    def test_restart_needs_exactly_the_planner_bound_fields(self):
        for parameters in ({"restart_token": self.proposal["parameters"]["restart_token"]},
                           {**self.proposal["parameters"], "note": "free text"},
                           {key: value for key, value in self.proposal["parameters"].items() if key != "template_sha256"},
                           {**self.proposal["parameters"], "template_sha256": "A" * 64},
                           {**self.proposal["parameters"], "template_sha256": "z" * 64},
                           {**self.proposal["parameters"], "restart_token": "restart now"},
                           {**self.proposal["parameters"], "restart_token": "RST-" + "A" * 16},
                           {**self.proposal["parameters"], "baseline_id": ""}):
            with self.subTest(parameters=parameters), self.assertRaises(ContractError):
                validate_message({**self.proposal, "parameters": parameters})

    def test_other_actions_keep_their_schemas(self):
        rollback = fixture("messages/proposal.json")
        validate_message(rollback)
        with self.assertRaises(ContractError):
            validate_message({**rollback, "parameters": {**rollback["parameters"], "restart_token": "RST-" + "0" * 16}})
        for action, parameters in (("SCALE", {"desired_replicas": 2}), ("ESCALATE", {}), ("NO_ACTION", {})):
            validate_message({**rollback, "action": action, "parameters": parameters})
        with self.assertRaises(ContractError):
            validate_message({**rollback, "action": "ESCALATE", "parameters": {"desired_replicas": 1}})


class RestartHelperTests(unittest.TestCase):
    def setUp(self):
        self.template = fixture("baseline.json")["pod_template"]

    def test_token_is_deterministic_and_bound_to_incident_and_baseline(self):
        token = restart_token("INC-1", "BASE-1")
        self.assertEqual(token, restart_token("INC-1", "BASE-1"))
        self.assertRegex(token, r"^RST-[a-f0-9]{16}$")
        self.assertNotEqual(token, restart_token("INC-2", "BASE-1"))
        self.assertNotEqual(token, restart_token("INC-1", "BASE-2"))

    def test_restart_template_changes_only_the_token_annotation(self):
        restarted = restart_template(self.template, "RST-" + "1" * 16)
        self.assertEqual(current_restart_token(restarted), "RST-" + "1" * 16)
        self.assertNotEqual(sha256_hex(restarted), sha256_hex(self.template))
        self.assertEqual(config_fingerprint(restarted), config_fingerprint(self.template))
        again = restart_template(restarted, "RST-" + "2" * 16)
        self.assertEqual(again, restart_template(self.template, "RST-" + "2" * 16))
        self.assertIsNone(current_restart_token(self.template))
        self.assertNotIn(RESTART_ANNOTATION, self.template.get("metadata", {}).get("annotations", {}))

    def test_config_fingerprint_sees_every_other_change(self):
        for mutate in (lambda t: t["spec"]["containers"][0]["env"].append({"name": "X", "value": "1"}),
                       lambda t: t["metadata"].setdefault("annotations", {}).update({"other.io/x": "1"}),
                       lambda t: t["metadata"]["labels"].update(app="other")):
            changed = deepcopy(self.template)
            mutate(changed)
            with self.subTest(changed=changed):
                self.assertNotEqual(config_fingerprint(changed), config_fingerprint(self.template))

    def test_empty_annotations_and_missing_metadata_normalise_alike(self):
        empty = deepcopy(self.template)
        empty["metadata"]["annotations"] = {}
        self.assertEqual(config_fingerprint(empty), config_fingerprint(self.template))
        bare = {"spec": deepcopy(self.template["spec"])}
        self.assertEqual(config_fingerprint(restart_template(bare, "RST-" + "3" * 16)), config_fingerprint(bare))
        with self.assertRaises(ValueError):
            config_fingerprint(["not", "a", "template"])
        with self.assertRaises(ValueError):
            restart_template(self.template, "")


class RestartGuardTests(unittest.TestCase):
    def setUp(self):
        self.inputs = case()

    def run_guard(self, approval=True, **changes):
        inputs = deepcopy(self.inputs)
        inputs.update(changes)
        if approval is True:
            approval = approval_for(inputs["proposal"], inputs["policy"])
        return evaluate_with_report(**inputs, approval=approval or None, now=NOW)

    def assert_verdict(self, verdict, code, **changes):
        decision, report = self.run_guard(**changes)
        self.assertEqual(decision["verdict"], verdict, decision["reason_codes"])
        self.assertIn(code, decision["reason_codes"])
        return decision, report

    def test_unchanged_configuration_with_failing_pods_is_allowed_with_approval(self):
        decision, report = self.assert_verdict("ALLOW", "VALIDATION_PASSED")
        codes = {item["code"]: item["status"] for item in report["checks"]}
        for code in ("RESTART_TARGET_ALLOWED", "RESTART_JUSTIFIED", "RESTART_BUDGET_AVAILABLE",
                     "RESTART_CONFIG_UNCHANGED", "RESTART_BINDING", "RESTART_TOKEN_FRESH", "BASELINE_IDENTITY_HASH"):
            self.assertEqual(codes[code], "PASS", code)
        self.assert_verdict("ESCALATE", "OPERATOR_APPROVAL_REQUIRED", approval=None)

    def test_restart_needs_the_action_and_a_restart_allowlisted_target(self):
        policy = deepcopy(self.inputs["policy"])
        policy["allowed_actions"] = ["ROLLBACK"]
        self.assert_verdict("REJECT", "ACTION_NOT_ALLOWED", policy=policy)
        policy = deepcopy(self.inputs["policy"])
        policy["restart_constraints"]["targets"] = []
        self.assert_verdict("REJECT", "RESTART_TARGET_NOT_ALLOWED", policy=policy)
        policy = deepcopy(self.inputs["policy"])
        del policy["restart_constraints"]
        decision, _ = self.assert_verdict("REJECT", "RESTART_TARGET_NOT_ALLOWED", policy=policy)

    def test_configuration_drift_is_rejected_so_restart_is_never_a_config_change(self):
        live = fixture("observations.json")["failed-rollout"][-1]
        live["restart_history"] = []
        proposal = restart_proposal(self.inputs["baseline"], live)
        decision, report = self.assert_verdict("REJECT", "RESTART_TEMPLATE_DRIFT",
                                               **with_proposal(self.inputs, proposal) | {"live_state": live})
        drift = next(item for item in report["checks"] if item["code"] == "RESTART_TEMPLATE_DRIFT")
        self.assertIn("use ROLLBACK", drift["message"])

    def test_a_live_template_already_carrying_the_token_is_rejected(self):
        live = deepcopy(self.inputs["live_state"])
        token = self.inputs["proposal"]["parameters"]["restart_token"]
        live["deployment"]["spec"]["template"] = restart_template(live["deployment"]["spec"]["template"], token)
        self.assert_verdict("REJECT", "RESTART_TOKEN_REUSED", live_state=live)

    def test_an_older_token_on_the_live_template_is_not_drift(self):
        live = deepcopy(self.inputs["live_state"])
        live["deployment"]["spec"]["template"] = restart_template(live["deployment"]["spec"]["template"],
                                                                  restart_token("INC-OLDER", "BASE-DEMO-001"))
        self.assert_verdict("ALLOW", "VALIDATION_PASSED", live_state=live)

    def test_parameters_not_bound_by_the_planner_are_rejected(self):
        baseline, live = self.inputs["baseline"], self.inputs["live_state"]
        other_token = restart_token("INC-OTHER", baseline["baseline_id"])
        for parameters in ({"restart_token": other_token,
                            "template_sha256": sha256_hex(restart_template(baseline["pod_template"], other_token))},
                           {"template_sha256": baseline["template_sha256"]},
                           {"template_sha256": "f" * 64}):
            with self.subTest(parameters=parameters):
                proposal = restart_proposal(baseline, live, **parameters)
                self.assert_verdict("REJECT", "RESTART_BINDING_MISMATCH", **with_proposal(self.inputs, proposal))
        proposal = restart_proposal(baseline, live, baseline_id="BASE-OTHER")
        self.assert_verdict("REJECT", "BASELINE_ID_MISMATCH", **with_proposal(self.inputs, proposal))

    def test_no_pod_failure_evidence_is_not_justified(self):
        healthy = fixture("observations.json")["healthy"][-1]
        healthy["restart_history"] = []
        proposal = restart_proposal(self.inputs["baseline"], healthy)
        self.assert_verdict("REJECT", "RESTART_NOT_JUSTIFIED", **with_proposal(self.inputs, proposal) | {"live_state": healthy})
        for change in ({"status": None}, {"status": {"readyReplicas": "0"}}):
            live = deepcopy(self.inputs["live_state"])
            live["deployment"].update(change)
            with self.subTest(change=change):
                self.assert_verdict("ESCALATE", "RESTART_JUSTIFICATION_UNKNOWN", live_state=live)
        # Either count short of desired is Pod-failure evidence, even when the other is complete.
        for ready, available in ((1, 0), (0, 1)):
            live = deepcopy(self.inputs["live_state"])
            live["deployment"]["status"].update(readyReplicas=ready, availableReplicas=available)
            with self.subTest(ready=ready, available=available):
                self.assert_verdict("ALLOW", "VALIDATION_PASSED", live_state=live)
        live = deepcopy(self.inputs["live_state"])
        live["deployment"]["metadata"]["generation"] = 2
        self.assert_verdict("ESCALATE", "RESTART_JUSTIFICATION_UNKNOWN", live_state=live)
        live = deepcopy(self.inputs["live_state"])
        live["deployment"]["spec"]["replicas"] = 0
        live["deployment"]["status"].update(replicas=0)
        self.assert_verdict("REJECT", "RESTART_NOT_JUSTIFIED", live_state=live)

    def test_restart_budget_counts_persisted_restarts_in_the_window(self):
        policy = deepcopy(self.inputs["policy"])
        policy["restart_constraints"]["max_restarts"] = 2
        recent = {"action_id": "ACT-EARLIER", "started_at": stamp(NOW - timedelta(minutes=30))}
        old = {"action_id": "ACT-OLD", "started_at": stamp(NOW - timedelta(hours=2))}
        future = {"action_id": "ACT-SKEWED", "started_at": stamp(NOW + timedelta(minutes=5))}
        for history, verdict, code in (([old, old, recent], "ALLOW", "VALIDATION_PASSED"),
                                       ([recent, recent], "REJECT", "RESTART_BUDGET_EXHAUSTED"),
                                       ([recent, future], "REJECT", "RESTART_BUDGET_EXHAUSTED")):
            live = deepcopy(self.inputs["live_state"])
            live["restart_history"] = history
            with self.subTest(history=history):
                self.assert_verdict(verdict, code, live_state=live, policy=policy)

    def test_missing_or_malformed_history_escalates_and_approval_cannot_clear_it(self):
        for history in (None, "none", [{"action_id": "A"}], [{"action_id": "A", "started_at": "yesterday"}],
                        [{"action_id": "A", "started_at": "2026-10-10T12:00:00+01:00"}]):
            live = deepcopy(self.inputs["live_state"])
            if history is None:
                del live["restart_history"]
            else:
                live["restart_history"] = history
            with self.subTest(history=history):
                self.assert_verdict("ESCALATE", "RESTART_BUDGET_UNKNOWN", live_state=live)

    def test_approval_cannot_rescue_a_hard_restart_rejection(self):
        live = deepcopy(self.inputs["live_state"])
        live["restart_history"] = [{"action_id": "ACT-EARLIER", "started_at": stamp(NOW - timedelta(minutes=1))}]
        decision, _ = self.assert_verdict("REJECT", "RESTART_BUDGET_EXHAUSTED", live_state=live)
        self.assertNotIn("OPERATOR_APPROVAL_REQUIRED", decision["reason_codes"])

    def test_privileged_or_over_budget_template_is_rejected_because_new_pods_use_it(self):
        baseline = deepcopy(self.inputs["baseline"])
        live = deepcopy(self.inputs["live_state"])
        for template in (baseline["pod_template"], live["deployment"]["spec"]["template"]):
            template["spec"]["containers"][0]["securityContext"]["privileged"] = True
        baseline["template_sha256"] = sha256_hex(baseline["pod_template"])
        proposal = restart_proposal(baseline, live)
        policy = deepcopy(self.inputs["policy"])
        policy["security_constraints"] = {"deny_privileged": True}
        inputs = with_proposal(self.inputs, proposal) | {"baseline": baseline, "live_state": live, "policy": policy}
        self.assert_verdict("REJECT", "SECURITY_PRIVILEGED", **inputs)
        policy = deepcopy(self.inputs["policy"])
        policy["workload_constraints"] = {"max_pod_requests": {"cpu": "10m"}}
        self.assert_verdict("REJECT", "WORKLOAD_POD_REQUEST_EXCEEDS_POLICY", policy=policy)

    def test_a_template_that_differs_only_by_a_token_is_already_at_baseline_for_rollback(self):
        rollback = fixture("messages/proposal.json")
        live = deepcopy(self.inputs["live_state"])
        live["deployment"]["metadata"]["resourceVersion"] = rollback["based_on_resource_version"]
        live["deployment"]["spec"]["template"] = restart_template(live["deployment"]["spec"]["template"],
                                                                  self.inputs["proposal"]["parameters"]["restart_token"])
        policy = fixture("policy.json")
        decision, _ = evaluate_with_report(rollback, live, policy, self.inputs["baseline"],
                                           fixture("messages/dependency-result.json"),
                                           approval_for(rollback, policy), now=NOW)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("ROLLBACK_ALREADY_AT_BASELINE", decision["reason_codes"])

    def test_scale_is_still_not_implemented(self):
        policy = deepcopy(self.inputs["policy"])
        policy["allowed_actions"].append("SCALE")
        proposal = bind({**self.inputs["proposal"], "action": "SCALE", "parameters": {"desired_replicas": 2}})
        self.assert_verdict("REJECT", "ACTION_NOT_IMPLEMENTED", **with_proposal(self.inputs, proposal) | {"policy": policy})

    def test_restart_policy_block_is_validated(self):
        for block in ({"targets": [], "max_restarts": 0, "window_seconds": 60},
                      {"targets": [], "max_restarts": 1, "window_seconds": 0},
                      {"targets": [], "max_restarts": 1},
                      {"targets": [{"name": "demo-api"}], "max_restarts": 1, "window_seconds": 60},
                      {"targets": "all", "max_restarts": 1, "window_seconds": 60},
                      {"targets": [], "max_restarts": True, "window_seconds": 60}):
            policy = deepcopy(self.inputs["policy"])
            policy["restart_constraints"] = block
            with self.subTest(block=block), self.assertRaises(GuardInputError):
                validate_policy(policy)
        live = fixture("policy.json")
        live.update(profile="live", allowed_actions=["ROLLBACK", "RESTART"])
        with self.assertRaisesRegex(GuardInputError, "restart_constraints"):
            guard_restart.validate_policy(live)
        guard_restart.validate_policy(dict(live, profile="development"))

    def test_a_restart_preview_must_cover_the_bound_candidate(self):
        proposal = self.inputs["proposal"]
        preview = {"status": "PASSED", "proposal_sha256": proposal_fingerprint(proposal),
                   "target": proposal["target"], "resource_version": proposal["based_on_resource_version"],
                   "template_sha256": proposal["parameters"]["template_sha256"], "checked_at": stamp(NOW),
                   "details_code": "SERVER_DRY_RUN_PASSED",
                   "admitted_template_sha256": proposal["parameters"]["template_sha256"]}
        policy = deepcopy(self.inputs["policy"])
        policy.update(require_server_dry_run=True, max_server_dry_run_age_seconds=10)
        inputs = deepcopy(self.inputs) | {"policy": policy}
        decision, _ = evaluate_with_report(**inputs, approval=approval_for(proposal, policy), now=NOW, preview=preview)
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])
        wrong = dict(preview, template_sha256=self.inputs["baseline"]["template_sha256"])
        decision, _ = evaluate_with_report(**inputs, approval=approval_for(proposal, policy), now=NOW, preview=wrong)
        self.assertIn("SERVER_DRY_RUN_MISMATCH", decision["reason_codes"])


class RestartExecutorTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.inputs = case()
        self.proposal = self.inputs["proposal"]
        live = deepcopy(self.inputs["live_state"])
        del live["restart_history"]
        self.cluster = FakeDeploymentAPI(live, clock=lambda: NOW)
        self.ledger = self.open_ledger()

    def open_ledger(self):
        ledger = SQLiteExecutionLedger(self.directory / "executor.db")
        self.addCleanup(ledger.close)
        return ledger

    def authorizer(self, proposal=None):
        proposal = proposal or self.proposal

        def dependencies(p):
            return dict(deepcopy(self.inputs["dependencies"]), incident_id=p["incident_id"], action_id=p["action_id"])
        return GuardAuthorizer(policy_reader=lambda p: deepcopy(self.inputs["policy"]),
                               baseline_reader=lambda p: deepcopy(self.inputs["baseline"]),
                               dependency_reader=dependencies,
                               approval_reader=lambda p: approval_for(p, self.inputs["policy"]))

    def adapters(self, **changes):
        values = dict(ledger=self.ledger, coordination=LocalTargetCoordinator(self.directory / "locks"),
                      cluster=self.cluster, authorizer=self.authorizer(), clock=lambda: NOW)
        values.update(changes)
        return ExecutionAdapters(**values)

    def test_restart_makes_one_conditional_write_and_replay_makes_none(self):
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "APPLIED", result["details"])
        self.assertEqual(len(self.cluster.write_attempts), 1)
        attempt = self.cluster.write_attempts[0]
        self.assertEqual(attempt["version"], self.proposal["based_on_resource_version"])
        self.assertEqual(attempt["template"], restart_template(self.inputs["baseline"]["pod_template"],
                                                               self.proposal["parameters"]["restart_token"]))
        self.assertEqual(attempt["evidence"]["action"], "RESTART")
        self.assertEqual(attempt["evidence"]["template_sha256"], self.proposal["parameters"]["template_sha256"])
        live = self.cluster.snapshot["deployment"]
        self.assertEqual(json.loads(live["metadata"]["annotations"][ACTION_ANNOTATION])["action"], "RESTART")
        self.assertEqual(execute(deepcopy(self.proposal), self.adapters()), result)
        self.assertEqual(reconcile(deepcopy(self.proposal), self.adapters()), result)
        self.assertEqual(len(self.cluster.write_attempts), 1)
        history = self.ledger.restart_history(self.proposal["target"])
        self.assertEqual([item["action_id"] for item in history], [self.proposal["action_id"]])

    def test_the_executor_refuses_drift_and_wrong_bindings_even_after_an_allow(self):
        class AllowAll:
            def __init__(self, baseline):
                self.baseline = baseline

            def validate(self, proposal, snapshot, *, now):
                decision = {"schema_version": "1.0", "kind": "guard_result", "incident_id": proposal["incident_id"],
                            "action_id": proposal["action_id"], "target": proposal["target"], "verdict": "ALLOW",
                            "reason_codes": ["VALIDATION_PASSED"], "policy_version": "test",
                            "validated_resource_version": snapshot["deployment"]["metadata"]["resourceVersion"],
                            "checked_at": stamp(now)}
                from services.executor.core import GuardCheck
                return GuardCheck(decision, deepcopy(self.baseline), {})

            def assert_usable(self, check, *, now):
                return True

        baseline = self.inputs["baseline"]
        live_template = self.cluster.snapshot["deployment"]["spec"]["template"]
        token = self.proposal["parameters"]["restart_token"]
        drifted = deepcopy(live_template)
        drifted["spec"]["containers"][0]["env"] = [{"name": "STARTUP_FAILURE", "value": "true"}]
        cases = {
            "drift": (self.proposal, drifted),
            "wrong hash": (restart_proposal(baseline, self.inputs["live_state"], template_sha256="e" * 64), live_template),
            "wrong token": (restart_proposal(baseline, self.inputs["live_state"],
                                             restart_token=restart_token("INC-X", baseline["baseline_id"])), live_template),
            "token reused": (self.proposal, restart_template(live_template, token)),
        }
        for name, (proposal, template) in cases.items():
            with self.subTest(case=name):
                self.cluster.snapshot["deployment"]["spec"]["template"] = deepcopy(template)
                result = execute(deepcopy(proposal), self.adapters(authorizer=AllowAll(baseline)))
                self.assertEqual(result["status"], "FAILED")
                self.assertIn("no write was attempted", result["details"])
        self.assertEqual(self.cluster.write_attempts, [])
        with self.assertRaises(ContractError):
            candidate_template({**self.proposal, "action": "SCALE"}, live_template, baseline)

    def test_handlers_refuse_a_tampered_baseline_without_the_guard(self):
        """Defence in depth: each handler checks the baseline itself, not only the Guard."""
        baseline = self.inputs["baseline"]
        live_template = self.cluster.snapshot["deployment"]["spec"]["template"]
        rollback = fixture("messages/proposal.json")
        self.assertEqual(candidate_template(rollback, live_template, baseline), baseline["pod_template"])
        tampered_template = deepcopy(baseline)
        tampered_template["pod_template"]["spec"]["containers"][0]["image"] = "tampered"
        cases = {
            "missing": None,
            "not an object": ["baseline"],
            "no template": {key: value for key, value in baseline.items() if key != "pod_template"},
            "template not an object": dict(baseline, pod_template="template"),
            "other target": dict(baseline, target=dict(baseline["target"], uid="other")),
            "other baseline": dict(baseline, baseline_id="BASE-OTHER"),
            "stale hash": dict(baseline, template_sha256="0" * 64),
            "edited template": tampered_template,
        }
        for proposal in (self.proposal, rollback):
            for name, stored in cases.items():
                with self.subTest(action=proposal["action"], case=name), self.assertRaises(ContractError):
                    candidate_template(proposal, live_template, deepcopy(stored))
        mismatched = dict(rollback, parameters=dict(rollback["parameters"], template_sha256="d" * 64))
        with self.assertRaises(ContractError):
            candidate_template(mismatched, live_template, baseline)

    def test_guard_refusal_at_execution_makes_no_write(self):
        healthy = fixture("observations.json")["healthy"][-1]
        self.cluster.snapshot["deployment"]["status"] = healthy["deployment"]["status"]
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("RESTART_NOT_JUSTIFIED", result["details"])
        self.assertEqual(self.cluster.write_attempts, [])

    def test_restart_budget_persists_across_an_executor_restart(self):
        first = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(first["status"], "APPLIED")
        # The Pods fail again, and a later incident asks for another restart of the same target.
        self.ledger.close()
        self.ledger = self.open_ledger()
        live = deepcopy(self.cluster.snapshot)
        second = restart_proposal(self.inputs["baseline"], live, "INC-RESTART-002")
        result = execute(deepcopy(second), self.adapters(authorizer=self.authorizer(second)))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("RESTART_BUDGET_EXHAUSTED", result["details"])
        self.assertEqual(len(self.cluster.write_attempts), 1)
        # A ROLLBACK is not counted against the restart budget.
        self.assertEqual(len(self.ledger.restart_history(self.proposal["target"])), 1)

    def record_past_writes(self, count, *, action="RESTART", started=NOW - timedelta(days=2)):
        """Ledger rows for earlier writes to this target, one second apart, oldest first."""
        for index in range(count):
            proposal = dict(self.proposal, action=action, action_id=f"ACT-PAST-{action}-{index:05d}")
            at = started + timedelta(seconds=index)
            self.ledger.db.execute(
                "INSERT INTO executor_actions(action_id,fingerprint,proposal,target_key,phase,write_started_at) "
                "VALUES(?,?,?,?,?,?)", (proposal["action_id"], "f" * 64, canonical_json(proposal),
                                        target_key(proposal["target"]), "DONE",
                                        at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")))

    def test_restart_history_is_the_newest_bounded_window_of_restarts(self):
        self.record_past_writes(RESTART_HISTORY_LIMIT + 5)
        self.record_past_writes(3, action="ROLLBACK", started=NOW - timedelta(hours=1))
        history = self.ledger.restart_history(self.proposal["target"])
        self.assertEqual(len(history), RESTART_HISTORY_LIMIT)
        self.assertEqual(history[0]["action_id"], "ACT-PAST-RESTART-00005")
        self.assertEqual(history[-1]["action_id"], f"ACT-PAST-RESTART-{RESTART_HISTORY_LIMIT + 4:05d}")
        self.assertEqual(history, sorted(history, key=lambda item: item["started_at"]))
        self.assertNotIn("ROLLBACK", json.dumps(history))

    def test_a_long_restart_history_outside_the_window_does_not_block_restart(self):
        # More lifetime restarts than the Guard accepts, all older than the one-hour window.
        self.record_past_writes(RESTART_HISTORY_LIMIT + 1)
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "APPLIED", result["details"])
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_a_long_restart_history_inside_the_window_still_exhausts_the_budget(self):
        self.record_past_writes(RESTART_HISTORY_LIMIT + 1, started=NOW - timedelta(minutes=30))
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("RESTART_BUDGET_EXHAUSTED", result["details"])
        self.assertEqual(self.cluster.write_attempts, [])

    def test_lost_response_reconciles_to_applied_from_restart_evidence(self):
        class LostResponse(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                super().conditional_restore(**kwargs)
                raise TimeoutError("response lost after commit")
        self.cluster = LostResponse(self.cluster.snapshot, clock=lambda: NOW)
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "APPLIED")
        self.assertTrue(result["details"].startswith("[basis=" + Basis.EVIDENCE_MATCH.value))
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_in_doubt_restart_stays_in_doubt_then_the_fence_settles_it(self):
        class LostRequest(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                self.write_attempts.append(deepcopy(kwargs))
                raise TimeoutError("request lost before the server")
        self.cluster = LostRequest(self.cluster.snapshot, clock=lambda: NOW)
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "NEEDS_RECONCILIATION")
        self.assertIn("UNCHANGED_IN_DOUBT", result["details"])
        later = NOW + timedelta(seconds=60)
        fenced = reconcile(deepcopy(self.proposal), self.adapters(clock=lambda: later, fence_after_seconds=30))
        self.assertEqual(fenced["status"], "CONFLICT")
        self.assertTrue(fenced["details"].startswith("[basis=FENCED]"))
        self.assertEqual(len(self.cluster.fences), 1)
        self.assertEqual(self.cluster.writes, [])
        self.assertIsNone(current_restart_token(self.cluster.snapshot["deployment"]["spec"]["template"]))
        # The write never committed, but it was sent, so it still uses the restart budget.
        self.assertEqual(len(self.ledger.restart_history(self.proposal["target"])), 1)

    def test_committed_restart_then_overwritten_is_superseded_without_a_second_write(self):
        class CrashAfterCommit(FakeDeploymentAPI):
            def conditional_restore(self, **kwargs):
                super().conditional_restore(**kwargs)
                raise ConditionalWriteConflict("misreported")
        self.cluster = CrashAfterCommit(self.cluster.snapshot, clock=lambda: NOW)
        result = execute(deepcopy(self.proposal), self.adapters())
        self.assertEqual(result["status"], "APPLIED")  # Adopted from evidence, not trusted from the response.
        self.ledger.close()
        self.ledger = self.open_ledger()
        row = self.ledger.get(self.proposal["action_id"])
        self.ledger.db.execute("UPDATE executor_actions SET phase='UNCERTAIN' WHERE action_id=?", (row["action_id"],))
        self.cluster.external_write(lambda deployment: deployment["spec"]["template"]["metadata"].update(
            annotations={"example.org/edit": "1"}))
        again = reconcile(deepcopy(self.proposal), self.adapters())
        self.assertEqual(again["status"], "SUPERSEDED")
        self.assertIn("COMMITTED_THEN_OVERWRITTEN", again["details"])
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_evidence_names_the_action_and_cannot_be_borrowed_across_actions(self):
        restart = action_evidence(self.proposal, sha256_hex(self.proposal), "a" * 32)
        self.assertEqual(restart["action"], "RESTART")
        self.assertTrue(evidence_fields_valid(restart))
        self.assertEqual(action_attempt(canonical_json(restart), self.proposal), "a" * 32)
        unnamed = {key: value for key, value in restart.items() if key != "action"}
        self.assertIsNone(action_attempt(canonical_json(unnamed), self.proposal))
        for wrong in ("ROLLBACK", "SCALE"):
            forged = dict(restart, action=wrong)
            self.assertIsNone(action_attempt(canonical_json(forged), self.proposal))
        rollback = fixture("messages/proposal.json")
        rollback_evidence = action_evidence(rollback, sha256_hex(rollback), "b" * 32)
        self.assertNotIn("action", rollback_evidence)
        self.assertEqual(action_attempt(canonical_json(rollback_evidence), rollback), "b" * 32)
        self.assertIsNone(action_attempt(canonical_json(dict(rollback_evidence, action="RESTART")), rollback))
        self.assertFalse(evidence_fields_valid(dict(rollback_evidence, action="ROLLBACK")))
        self.assertFalse(evidence_fields_valid([]))


class RestartPlannerTests(unittest.TestCase):
    def setUp(self):
        self.policy = restart_policy(fixture("policy.json"))
        self.baseline = fixture("baseline.json")
        self.observations = fixture("observations.json")["pod-failure"]
        self.incident = detect(self.observations, self.policy)
        self.live_template = self.observations[-1]["deployment"]["spec"]["template"]

    def plan(self, provider=mock_provider, live_template="live", incident=None):
        live_template = self.live_template if live_template == "live" else live_template
        return propose(incident or self.incident, [self.baseline], retrieve_runbooks, provider,
                       live_template=live_template)

    def test_mock_provider_proposes_a_planner_bound_restart(self):
        proposal = self.plan()
        self.assertEqual(proposal["action"], "RESTART")
        self.assertEqual(proposal["parameters"], restart_parameters(self.incident["incident_id"], self.baseline))
        self.assertEqual(proposal, self.plan())  # Deterministic identity and parameters.
        self.assertEqual([item["id"] for item in retrieve_runbooks(self.incident)], ["failed-rollout", "pod-failure"])

    def test_without_a_trusted_live_read_or_with_drift_the_mock_provider_rolls_back(self):
        self.assertEqual(self.plan(live_template=None)["action"], "ROLLBACK")
        failed = fixture("observations.json")["failed-rollout"]
        incident = detect(failed, self.policy)
        proposal = self.plan(incident=incident, live_template=failed[-1]["deployment"]["spec"]["template"])
        self.assertEqual(proposal["action"], "ROLLBACK")
        self.assertEqual([item["id"] for item in retrieve_runbooks(incident)], ["failed-rollout"])

    def test_restart_parameters_from_a_provider_are_refused(self):
        def provider(context):
            return {"action": "RESTART", "parameters": restart_parameters(context["incident"]["incident_id"],
                                                                          context["baselines"][0]),
                    "rationale": "computed by the provider", "evidence_refs": []}
        with self.assertRaisesRegex(ContractError, "leave them empty"):
            self.plan(provider)

    def test_restart_without_an_unchanged_configuration_is_refused(self):
        def provider(context):
            return {"action": "RESTART", "parameters": {}, "rationale": "restart", "evidence_refs": []}
        drifted = deepcopy(self.live_template)
        drifted["spec"]["containers"][0]["image"] = "sentinel-demo:9.9.9"
        for live in (None, drifted):
            with self.subTest(live=live is not None), self.assertRaisesRegex(ContractError, "unchanged|equals"):
                self.plan(provider, live_template=live)
        with self.assertRaises(ContractError):
            self.plan(provider, live_template=["not", "a", "template"])

    def test_the_agent_path_cannot_restart_without_a_trusted_read(self):
        def provider(context):
            return {"action": "RESTART", "parameters": {}, "rationale": "restart"}
        diagnosis = {"incident_id": self.incident["incident_id"],
                     "prediction": {"1": {"root cause component": "demo-api"}}}
        with self.assertRaises(ContractError):
            propose(self.incident, [self.baseline], lambda query: {"kp": retrieve_runbooks(self.incident)},
                    provider, diagnosis=diagnosis)


class RestartCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        self.incident = fixture("messages/incident.json")
        live = fixture("observations.json")["pod-failure"][-1]
        self.restart = restart_proposal(fixture("baseline.json"), live, self.incident["incident_id"])

    def reject(self, proposal):
        guard = fixture("messages/guard-result.json")
        guard.update(action_id=proposal["action_id"], verdict="REJECT", reason_codes=["RESTART_NOT_JUSTIFIED"],
                     validated_resource_version=proposal["based_on_resource_version"])
        handle(guard, self.store, self.bus)

    def test_a_second_restart_for_one_incident_is_refused_but_rollback_is_accepted(self):
        handle(self.incident, self.store, self.bus)
        handle(self.restart, self.store, self.bus)
        self.reject(self.restart)
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "REJECTED")
        again = bind(dict(self.restart, rationale="Try the restart once more."))
        with self.assertRaisesRegex(CoordinatorError, "Restart budget"):
            handle(again, self.store, self.bus)
        rollback = fixture("messages/proposal.json")
        self.assertTrue(handle(rollback, self.store, self.bus))
        self.assertEqual(self.store.incident(self.incident["incident_id"])["state"], "VALIDATING")
        self.assertEqual([action["proposal"]["action"] for action in
                          self.store.actions_for_incident(self.incident["incident_id"])], ["RESTART", "ROLLBACK"])


class RestartFlowTests(unittest.TestCase):
    def flow(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        flow = OfflineFlow(directory.name, scenario="pod-failure")
        self.addCleanup(flow.close)
        return flow

    def test_demo_restart_reaches_recovered_with_one_write(self):
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            demonstrate_restart()
        summary = json.loads(output.getvalue())
        self.assertEqual({key: summary[key] for key in ("action", "status", "writes", "restart_history")},
                         {"action": "RESTART", "status": "RECOVERED", "writes": 1, "restart_history": 1})

    def test_restart_budget_persists_across_component_restarts(self):
        flow = self.flow()
        flow.prepare()
        flow.authorize()
        flow.execute()
        self.assertEqual(flow.state(), "VERIFYING")
        flow.now = datetime(2026, 10, 10, 12, 1, 10, tzinfo=UTC)
        flow.verify(fixture("health.json")["stable"])
        self.assertEqual(flow.state(), "RECOVERED")
        # Every store is reopened, then the Pods fail again under a new incident.
        flow.restart()
        flow.ledger.close()
        flow.ledger = SQLiteExecutionLedger(flow.path / "executor.db")
        flow.adapters.ledger = flow.ledger
        failing = deepcopy(fixture("observations.json")["pod-failure"])
        version = flow.cluster.snapshot["deployment"]["metadata"]["resourceVersion"]
        restarted = flow.cluster.snapshot["deployment"]["spec"]["template"]
        for offset, item in enumerate(failing):
            item["captured_at"] = stamp(datetime(2026, 10, 10, 12, 2, tzinfo=UTC) + timedelta(seconds=15 * offset))
            item["deployment"]["metadata"]["resourceVersion"] = version
            item["deployment"]["spec"]["template"] = deepcopy(restarted)
        flow.now = datetime(2026, 10, 10, 12, 2, 31, tzinfo=UTC)
        incident = detect(failing, flow.policy)
        self.assertNotEqual(incident["incident_id"], flow.incident["incident_id"])
        flow.cluster.snapshot["deployment"]["status"] = failing[-1]["deployment"]["status"]
        handle(incident, flow.store, flow.bus)
        second = flow.plan()[0]
        self.assertEqual(second["action"], "RESTART")
        self.assertNotEqual(second["parameters"]["restart_token"], flow.proposal["parameters"]["restart_token"])
        flow.proposal, flow.incident = second, incident
        flow.guard()
        action = flow.store.action(second["action_id"])
        self.assertEqual(flow.state(), "REJECTED")
        self.assertIn("RESTART_BUDGET_EXHAUSTED", action["guard_result"]["reason_codes"])
        self.assertEqual(len(flow.cluster.writes), 1)

    def test_restart_verification_binds_the_restart_template(self):
        for template, expected in ((None, "RECOVERED"), ("baseline", "NOT_RECOVERED")):
            flow = self.flow()
            flow.prepare()
            flow.authorize()
            flow.execute()
            execution = flow.store.action(flow.proposal["action_id"])["execution_result"]
            observed = flow.proposal["parameters"]["template_sha256"] if template is None else flow.baseline["template_sha256"]
            at = datetime.fromisoformat(execution["recorded_at"].replace("Z", "+00:00"))
            samples = [{"at": stamp(at + timedelta(seconds=offset)), "service": "demo-api",
                        "target": deepcopy(execution["target"]), "http_status": 200, "ready_replicas": 1,
                        "desired_replicas": 1, "available_replicas": 1, "dependencies": {"mock-db": True},
                        "configuration_stable": True, "template_sha256": observed} for offset in (1, 11, 21)]
            policy = deepcopy(flow.policy)
            policy["health"].update(require_template_match=True, require_target_identity=True,
                                    require_available_replicas=True)
            flow.now = at + timedelta(seconds=25)
            process_verifier_requests(flow.store, flow.bus, ledger=flow.verification,
                                      sample_reader=lambda execution, samples=samples: deepcopy(samples),
                                      policy_reader=lambda execution, policy=policy: deepcopy(policy),
                                      clock=lambda flow=flow: flow.now)
            with self.subTest(template=template):
                self.assertEqual(flow.store.incident(flow.incident["incident_id"])["state"],
                                 "RECOVERED" if expected == "RECOVERED" else "FAILED")

    def test_rollback_flow_is_unchanged_by_the_template_reader(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        flow = OfflineFlow(directory.name)
        self.addCleanup(flow.close)
        flow.prepare()
        self.assertEqual(flow.proposal["action"], "ROLLBACK")
        self.assertEqual(flow.proposal, fixture_rollback_from(flow))


def fixture_rollback_from(flow):
    """The ROLLBACK the Planner made before RESTART existed: same incident, same baseline."""
    message = {"schema_version": "1.0", "kind": "proposal", "incident_id": flow.incident["incident_id"],
               "target": flow.incident["target"],
               "based_on_resource_version": flow.incident["observed_resource_version"],
               "action": "ROLLBACK",
               "parameters": {key: flow.baseline[key] for key in ("baseline_id", "template_sha256")},
               "rationale": "Restore the saved observed healthy template described by the failed-rollout runbook.",
               "evidence_refs": [item["evidence_id"] for item in flow.incident["signals"]]}
    message["action_id"] = "ACT-" + sha256_hex(message)[:24]
    return message


class RestartAdapterAndSampleTests(unittest.TestCase):
    def test_kubectl_receives_one_conditional_patch_with_the_token_and_named_evidence(self):
        from services.executor.kubernetes_adapter import KubectlDeploymentAPI, LiveAdapterError
        from tests.test_k8s_executor_adapter import Runner
        inputs = case()
        proposal, baseline = inputs["proposal"], inputs["baseline"]
        deployment = inputs["live_state"]["deployment"]
        runner = Runner(deployment)
        adapter = KubectlDeploymentAPI(context="kind-team", cluster_id=proposal["target"]["cluster_id"],
                                       kubeconfig="restricted.json", allowed_targets=[proposal["target"]],
                                       runner=runner, clock=lambda: NOW)
        template = candidate_template(proposal, deployment["spec"]["template"], baseline)
        evidence = action_evidence(proposal, sha256_hex(proposal), "c" * 32)
        version = deployment["metadata"]["resourceVersion"]
        adapter.conditional_restore(target=proposal["target"], version=version, template=template, evidence=evidence)
        self.assertEqual(len(runner.patches), 1)
        patch = runner.patches[0]
        self.assertEqual(patch[:3], [{"op": "test", "path": "/metadata/uid", "value": proposal["target"]["uid"]},
                                     {"op": "test", "path": "/metadata/resourceVersion", "value": version},
                                     {"op": "replace", "path": "/spec/template", "value": template}])
        self.assertEqual(patch[2]["value"]["metadata"]["annotations"][RESTART_ANNOTATION],
                         proposal["parameters"]["restart_token"])
        written = patch[3]["value"]
        written = written[ACTION_ANNOTATION] if isinstance(written, dict) else written
        self.assertEqual(json.loads(written)["action"], "RESTART")
        for bad in (dict(evidence, action="SCALE"), dict(evidence, action="ROLLBACK"), dict(evidence, extra="x")):
            with self.subTest(evidence=bad), self.assertRaises(LiveAdapterError):
                adapter.conditional_restore(target=proposal["target"], version=version, template=template, evidence=bad)
        self.assertEqual(len(runner.patches), 1)

    def test_contract_check_refuses_an_unbound_restart_sample(self):
        import contextlib
        import importlib.util
        import io
        import shutil
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location("check_contracts_restart", ROOT / "scripts/check_contracts.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        samples = root / "data/sentinel/samples"
        shutil.copytree(ROOT / "data/sentinel/samples/messages", samples / "messages")
        shutil.copyfile(ROOT / "data/sentinel/samples/baseline.json", samples / "baseline.json")

        def run():
            with patch.object(module, "ROOT", root), contextlib.redirect_stdout(io.StringIO()) as out:
                module.main()
            return out.getvalue()
        self.assertIn("Validated 7 example messages", run())
        path = samples / "messages/restart-proposal.json"
        sample = json.loads(path.read_text(encoding="utf-8"))
        sample["parameters"]["restart_token"] = restart_token("INC-OTHER", sample["parameters"]["baseline_id"])
        path.write_text(json.dumps(sample), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "RESTART sample is not bound"):
            run()


class RestartModelCheckTests(unittest.TestCase):
    """The in-flight write model checker, run with a RESTART instead of a ROLLBACK."""

    def test_invariants_hold_for_restart(self):
        inputs = case()
        fixtures = {"proposal": inputs["proposal"], "baseline": inputs["baseline"],
                    "live_state": fixture("observations.json")["pod-failure"][-1]}
        report = model.check(range(600), fixtures=fixtures)
        failing = [code for code in model.SAFETY + model.LIVENESS + model.ADOPTION + model.FENCING
                   if report.failures[code]]
        self.assertEqual(failing, [], "\n\n".join(model.explain(report.first[code], code) for code in failing))
        for status in ("APPLIED/EVIDENCE_MATCH", "CONFLICT/FENCED", "SUPERSEDED/COMMITTED_THEN_OVERWRITTEN",
                       "NEEDS_RECONCILIATION/UNCHANGED_IN_DOUBT"):
            self.assertGreater(report.observed[status], 0, status)


if __name__ == "__main__":
    unittest.main()
