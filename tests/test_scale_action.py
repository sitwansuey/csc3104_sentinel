"""SCALE as 'restore the reviewed replica count', end to end and offline (P5.07).

Real Observer, Planner, Coordinator, Guard, Executor and Verifier code with a
fake Deployment API and a scripted kubectl. A SCALE writes spec.replicas, and
nothing else, through a conditional JSON Patch with UID and resourceVersion
tests and action evidence. Nothing here touches a cluster.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from experiments.boutique.harness import scale_recovery, scenarios, targets
from experiments.boutique.harness.scenarios import CatalogError
from runtime.executor_pipeline import GuardAuthorizer, process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.planner_pipeline import process_planner_requests
from runtime.verifier_pipeline import _bind_expectations, process_verifier_requests
from services.coordinator.core import CoordinatorError, LocalEventBus, SQLiteStore, approve, handle
from services.executor.actions import evidence as action_evidence, scale_replicas
from services.executor.core import ACTION_ANNOTATION, ConditionalWriteConflict, ExecutionAdapters, execute, reconcile
from services.executor.kubernetes_adapter import (
    KubectlDeploymentAPI, LiveAdapterError, WriteOutcomeUnknown,
)
from services.executor.core import ReadUnavailable
from services.executor.reconciliation import Basis, action_attempt, classify, evidence_fields_valid, observe
from services.executor.store import LocalTargetCoordinator, SQLiteExecutionLedger, canonical_json, fingerprint
from services.guard import scale as guard_scale
from services.guard.core import GuardInputError, evaluate_with_report, proposal_fingerprint
from services.guard.decisions import validate_policy
from services.guard.preview import server_dry_run
from services.observer.core import Observer, ObservationError, REPLICAS_BELOW_MINIMUM, detect
from services.planner.core import mock_provider, propose, retrieve_runbooks
from services.planner.store import PlannerStore
from services.verifier.core import VerifierInputError, inspect_dependencies, verify
from services.verifier.store import VerificationStore
from shared.actions import (
    IMPLEMENTED_ACTIONS, TEMPLATE_ACTIONS, baseline_replicas, restart_template, scale_parameters,
)
from shared.canonical import sha256_hex
from shared.contracts import ContractError, validate_message
from tests.support.executor_fake import FakeDeploymentAPI
from tests.test_k8s_executor_adapter import Runner

ROOT = Path(__file__).resolve().parents[1]
START = datetime(2026, 10, 10, 12, 0, 0, tzinfo=UTC)
NOW = datetime(2026, 10, 10, 12, 0, 40, tzinfo=UTC)
KEY = "sentinel-demo/demo-api"


def fixture(name):
    return json.loads((ROOT / "data/sentinel/samples" / name).read_text(encoding="utf-8"))


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


def baseline(replicas=1):
    value = fixture("baseline.json")
    if replicas is not None:
        value["replicas"] = replicas
    return value


def scaled(observation, replicas, *, at, version="201", generation=2):
    """An observation of the fixture Deployment scaled to replicas, with all its Pods gone at zero."""
    observation = deepcopy(observation)
    observation["captured_at"] = stamp(at)
    deployment = observation["deployment"]
    deployment["metadata"].update(resourceVersion=version, generation=generation)
    deployment["spec"]["replicas"] = replicas
    deployment["spec"]["template"] = deepcopy(fixture("baseline.json")["pod_template"])
    deployment["status"] = {"observedGeneration": generation, "replicas": replicas,
                            "readyReplicas": replicas, "availableReplicas": replicas}
    if replicas == 0:
        observation.update(pods=[], events=[], logs=[])
    return observation


def scaled_down(count=3, step=15):
    """Observer samples of the Deployment at 0 replicas, step seconds apart."""
    healthy = fixture("observations.json")["healthy"][-1]
    return [scaled(healthy, 0, at=START + timedelta(seconds=index * step)) for index in range(count)]


def scale_policy(policy=None):
    policy = deepcopy(policy or fixture("policy.json"))
    policy["allowed_actions"] = [*policy["allowed_actions"], "SCALE"]
    policy["scale_constraints"] = {"targets": deepcopy(policy["targets"])}
    policy["workload_constraints"] = {"max_replicas": 2}
    policy.setdefault("detection", {})["expected_min_replicas"] = {KEY: 1}
    return policy


def bind(message):
    message = {key: value for key, value in message.items() if key != "action_id"}
    message["action_id"] = "ACT-" + sha256_hex(message)[:24]
    return message


def scale_proposal(live, base=None, *, incident_id="INC-SCALE-001", **parameters):
    base = base or baseline()
    bound = {"baseline_id": base["baseline_id"], "desired_replicas": 1,
             "template_sha256": fingerprint(live["deployment"]["spec"]["template"])}
    bound.update(parameters)
    return bind({"schema_version": "1.0", "kind": "proposal", "incident_id": incident_id,
                 "target": deepcopy(base["target"]), "action": "SCALE", "parameters": bound,
                 "based_on_resource_version": live["deployment"]["metadata"]["resourceVersion"],
                 "rationale": "Restore the reviewed replica count.", "evidence_refs": ["E-SCALE"]})


def live_state(replicas=0, *, autoscalers=()):
    state = scaled(fixture("observations.json")["healthy"][-1], replicas, at=NOW - timedelta(seconds=5))
    if autoscalers is not None:
        state["autoscalers"] = [{"name": name} for name in autoscalers]
    return state


def approval_for(proposal, policy):
    return {"action_id": proposal["action_id"], "proposal_sha256": proposal_fingerprint(proposal),
            "policy_version": policy["policy_version"], "operator_id": "fixture-operator",
            "decision": "APPROVED", "approved_at": "2026-10-10T12:00:35Z", "expires_at": "2026-10-10T12:01:35Z"}


def dependencies_for(proposal):
    result = fixture("messages/dependency-result.json")
    result.update(incident_id=proposal["incident_id"], action_id=proposal["action_id"],
                  target=deepcopy(proposal["target"]))
    return result


def guard_case(*, replicas=0, base=None, policy=None, autoscalers=(), approved=True, **parameters):
    base = baseline() if base is None else base
    live = live_state(replicas, autoscalers=autoscalers)
    proposal = scale_proposal(live, base, **parameters)
    policy = policy or scale_policy()
    return {"proposal": proposal, "live_state": live, "policy": policy, "baseline": base,
            "dependencies": dependencies_for(proposal),
            "approval": approval_for(proposal, policy) if approved else None}


def decide(case):
    return evaluate_with_report(**case, now=NOW)


def codes(report, status):
    return {item["code"] for item in report["checks"] if item["status"] == status}


class ScaleRegistryTests(unittest.TestCase):
    def test_scale_is_implemented_but_writes_no_template(self):
        self.assertIn("SCALE", IMPLEMENTED_ACTIONS)
        self.assertNotIn("SCALE", TEMPLATE_ACTIONS)

    def test_baseline_replicas_accepts_only_a_reviewed_count(self):
        self.assertEqual(baseline_replicas({"replicas": 3}), 3)
        for value in (0, -1, 11, True, "1", None, 1.0):
            with self.subTest(value=value):
                self.assertIsNone(baseline_replicas({"replicas": value}))
        self.assertIsNone(baseline_replicas({}))
        self.assertIsNone(baseline_replicas(None))

    def test_scale_parameters_bind_the_baseline_count_and_the_live_template(self):
        base = baseline(2)
        live = restart_template(base["pod_template"], "RST-0123456789abcdef")
        self.assertEqual(scale_parameters(base, live), {"baseline_id": base["baseline_id"], "desired_replicas": 2,
                                                        "template_sha256": fingerprint(live)})
        with self.assertRaises(ValueError):
            scale_parameters(baseline(None), base["pod_template"])
        drifted = deepcopy(base["pod_template"])
        drifted["spec"]["containers"][0]["image"] = "other@sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            scale_parameters(base, drifted)


class ObserverScaleTests(unittest.TestCase):
    def test_zero_replicas_without_a_reviewed_minimum_is_still_intentional(self):
        self.assertIsNone(detect(scaled_down(), {"detection": {"failure_duration_seconds": 30}}))

    def test_a_count_below_the_reviewed_minimum_raises_an_incident_after_the_failure_duration(self):
        policy = {"detection": {"failure_duration_seconds": 30, "expected_min_replicas": {KEY: 1}}}
        self.assertIsNone(detect(scaled_down(2), policy))  # 15 s of failure is not yet 30 s.
        incident = detect(scaled_down(3), policy)
        validate_message(incident)
        reasons = [signal["reason"] for signal in incident["signals"]]
        self.assertIn(REPLICAS_BELOW_MINIMUM, reasons)
        signal = next(item for item in incident["signals"] if item["reason"] == REPLICAS_BELOW_MINIMUM)
        self.assertEqual(signal["source"], "deployment_status")
        self.assertIn("below the reviewed minimum 1", signal["detail"])

    def test_a_count_at_the_minimum_or_for_another_target_raises_nothing(self):
        healthy = fixture("observations.json")["healthy"][-1]
        at_minimum = [scaled(healthy, 1, at=START + timedelta(seconds=index * 15)) for index in range(3)]
        self.assertIsNone(detect(at_minimum, {"detection": {"failure_duration_seconds": 30,
                                                            "expected_min_replicas": {KEY: 1}}}))
        self.assertIsNone(detect(scaled_down(), {"detection": {"failure_duration_seconds": 30,
                                                               "expected_min_replicas": {"other/demo-api": 1}}}))

    def test_malformed_minimums_are_refused(self):
        for value in ([], {"demo-api": 1}, {"/demo-api": 1}, {"a/b/c": 1}, {KEY: 0}, {KEY: 11}, {KEY: True},
                      {KEY: "1"}):
            with self.subTest(value=value), self.assertRaises(ObservationError):
                Observer({"detection": {"expected_min_replicas": value}})

    def test_checkpoint_carries_the_minimum_only_when_one_is_configured(self):
        plain = Observer({"detection": {"failure_duration_seconds": 30}})
        self.assertNotIn("expected_min_replicas", plain.export_state())
        policy = {"detection": {"failure_duration_seconds": 30, "expected_min_replicas": {KEY: 1}}}
        observer = Observer(policy)
        for observation in scaled_down(2):
            observer.observe(observation)
        state = observer.export_state()
        self.assertEqual(state["expected_min_replicas"], {KEY: 1})
        resumed = Observer.from_state(policy, json.loads(json.dumps(state)))
        self.assertIsNotNone(resumed.observe(scaled_down(3)[-1]))
        with self.assertRaises(ObservationError):
            Observer.from_state({"detection": {"failure_duration_seconds": 30}}, state)
        with self.assertRaises(ObservationError):
            Observer.from_state(policy, plain.export_state())


class PlannerScaleTests(unittest.TestCase):
    def incident(self):
        return detect(scaled_down(), {"detection": {"failure_duration_seconds": 30,
                                                    "expected_min_replicas": {KEY: 1}}})

    def test_runbook_retrieval_selects_the_scale_guidance(self):
        self.assertIn("replicas-below-minimum", [item["id"] for item in retrieve_runbooks(self.incident())])

    def test_the_rule_provider_proposes_scale_and_the_planner_binds_it(self):
        base = baseline()
        proposal = propose(self.incident(), [base], retrieve_runbooks, mock_provider,
                           live_template=base["pod_template"])
        self.assertEqual(proposal["action"], "SCALE")
        self.assertEqual(proposal["parameters"], scale_parameters(base, base["pod_template"]))
        validate_message(proposal)

    def test_without_a_reviewed_count_or_trusted_template_no_scale_is_proposed(self):
        incident = self.incident()
        base = baseline(None)
        self.assertNotEqual(propose(incident, [base], retrieve_runbooks, mock_provider,
                                    live_template=base["pod_template"])["action"], "SCALE")
        self.assertNotEqual(propose(incident, [baseline()], retrieve_runbooks, mock_provider)["action"], "SCALE")

    def test_a_provider_cannot_choose_the_count_or_scale_a_drifted_template(self):
        base = baseline()
        chosen = lambda context: {**mock_provider(context), "parameters": {"desired_replicas": 5}}  # noqa: E731
        with self.assertRaises(ContractError):
            propose(self.incident(), [base], retrieve_runbooks, chosen, live_template=base["pod_template"])
        drifted = deepcopy(base["pod_template"])
        drifted["spec"]["containers"][0]["image"] = "other@sha256:" + "0" * 64
        forced = lambda context: {**mock_provider({**context, "template_unchanged": True,  # noqa: E731
                                                   "baseline_replicas": 1})}
        with self.assertRaises(ContractError):
            propose(self.incident(), [base], retrieve_runbooks, forced, live_template=drifted)


class GuardScaleTests(unittest.TestCase):
    def test_restoring_the_baseline_count_is_allowed(self):
        decision, report = decide(guard_case())
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])
        self.assertLessEqual({"SCALE_TARGET_ALLOWED", "SCALE_RESTORES_BASELINE", "SCALE_RAISES_REPLICAS",
                              "SCALE_CONFIG_UNCHANGED", "SCALE_BINDING", "SCALE_NO_AUTOSCALER"}, codes(report, "PASS"))

    def test_without_approval_it_escalates_for_the_operator_only(self):
        decision, _ = decide(guard_case(approved=False))
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("ESCALATE", ["OPERATOR_APPROVAL_REQUIRED"]))

    def test_a_count_other_than_the_baseline_is_rejected(self):
        decision, _ = decide(guard_case(desired_replicas=2))
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertEqual(decision["reason_codes"], ["SCALE_NOT_BASELINE"])

    def test_a_count_over_the_policy_ceiling_is_rejected(self):
        decision, _ = decide(guard_case(desired_replicas=5))
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn("SCALE_NOT_BASELINE", decision["reason_codes"])
        self.assertIn("WORKLOAD_REPLICAS_EXCEED_POLICY", decision["reason_codes"])
        # Even a baseline above the ceiling cannot be restored.
        decision, _ = decide(guard_case(base=baseline(3), desired_replicas=3))
        self.assertEqual(decision["reason_codes"], ["WORKLOAD_REPLICAS_EXCEED_POLICY"])

    def test_no_change_and_a_reduction_are_rejected(self):
        decision, _ = decide(guard_case(replicas=1))
        self.assertEqual(decision["reason_codes"], ["SCALE_NO_CHANGE"])
        decision, _ = decide(guard_case(replicas=2, base=baseline(1)))
        self.assertEqual(decision["reason_codes"], ["SCALE_WOULD_REDUCE"])

    def test_a_drifted_template_is_rejected_for_rollback_first(self):
        case = guard_case()
        template = case["live_state"]["deployment"]["spec"]["template"]
        template["spec"]["containers"][0]["env"] = [{"name": "PORT", "value": "invalid-port"}]
        case["proposal"] = bind({**case["proposal"], "parameters": {**case["proposal"]["parameters"],
                                                                     "template_sha256": fingerprint(template)}})
        case["approval"] = approval_for(case["proposal"], case["policy"])
        case["dependencies"] = dependencies_for(case["proposal"])
        decision, _ = decide(case)
        self.assertEqual(decision["reason_codes"], ["SCALE_TEMPLATE_DRIFT"])

    def test_a_template_other_than_the_live_one_is_a_binding_mismatch(self):
        decision, _ = decide(guard_case(template_sha256="c" * 64))
        self.assertEqual(decision["reason_codes"], ["SCALE_BINDING_MISMATCH"])

    def test_a_restart_token_on_the_live_template_is_not_drift(self):
        case = guard_case()
        template = restart_template(case["live_state"]["deployment"]["spec"]["template"], "RST-0123456789abcdef")
        case["live_state"]["deployment"]["spec"]["template"] = template
        case["proposal"] = bind({**case["proposal"], "parameters": {**case["proposal"]["parameters"],
                                                                     "template_sha256": fingerprint(template)}})
        case["approval"] = approval_for(case["proposal"], case["policy"])
        case["dependencies"] = dependencies_for(case["proposal"])
        self.assertEqual(decide(case)[0]["verdict"], "ALLOW")

    def test_an_autoscaler_or_an_unknown_one_escalates(self):
        decision, report = decide(guard_case(autoscalers=("cart-hpa",)))
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("ESCALATE", ["SCALE_AUTOSCALER_PRESENT"]))
        decision, _ = decide(guard_case(autoscalers=None))
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("ESCALATE", ["SCALE_AUTOSCALER_UNKNOWN"]))
        for value in ("none", [{"name": ""}], [{"name": "a", "extra": 1}], [{"name": "a"}] * 101):
            case = guard_case()
            case["live_state"]["autoscalers"] = value
            with self.subTest(value=str(value)[:40]):
                self.assertEqual(decide(case)[0]["reason_codes"], ["SCALE_AUTOSCALER_UNKNOWN"])

    def test_a_target_outside_scale_constraints_is_rejected(self):
        policy = scale_policy()
        policy["scale_constraints"]["targets"] = []
        decision, _ = decide(guard_case(policy=policy))
        self.assertEqual(decision["reason_codes"], ["SCALE_TARGET_NOT_ALLOWED"])
        policy = scale_policy()
        del policy["scale_constraints"]
        self.assertEqual(decide(guard_case(policy=policy))[0]["reason_codes"], ["SCALE_TARGET_NOT_ALLOWED"])

    def test_missing_reviewed_count_or_ceiling_escalates(self):
        decision, _ = decide(guard_case(base=baseline(None)))
        self.assertEqual((decision["verdict"], decision["reason_codes"]),
                         ("ESCALATE", ["SCALE_BASELINE_REPLICAS_UNKNOWN"]))
        policy = scale_policy()
        del policy["workload_constraints"]
        decision, _ = decide(guard_case(policy=policy))
        self.assertEqual((decision["verdict"], decision["reason_codes"]), ("ESCALATE", ["SCALE_REPLICA_LIMIT_UNKNOWN"]))

    def test_an_unobserved_generation_escalates(self):
        case = guard_case()
        case["live_state"]["deployment"]["status"]["observedGeneration"] = 1
        self.assertEqual(decide(case)[0]["reason_codes"], ["SCALE_STATE_UNKNOWN"])

    def test_a_scale_bound_to_another_baseline_is_rejected_by_the_core(self):
        decision, _ = decide(guard_case(baseline_id="BASE-OTHER"))
        self.assertEqual(decision["reason_codes"], ["BASELINE_ID_MISMATCH"])

    def test_policy_block_is_validated_and_required_for_live_scale(self):
        for block in ({}, {"targets": "all"}, {"targets": [{"name": "demo-api"}]}, {"targets": [], "extra": 1}):
            policy = scale_policy()
            policy["scale_constraints"] = block
            with self.subTest(block=block), self.assertRaises(GuardInputError):
                validate_policy(policy)
        live = scale_policy()
        live["profile"] = "live"
        del live["scale_constraints"]
        with self.assertRaisesRegex(GuardInputError, "scale_constraints"):
            guard_scale.validate_policy(live)

    def test_autoscalers_for_filters_a_kubectl_list(self):
        listing = {"items": [
            {"metadata": {"name": "b"}, "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment",
                                                                    "name": "demo-api"}}},
            {"metadata": {"name": "a"}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "demo-api"}}},
            {"metadata": {"name": "c"}, "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "other"}}},
            {"metadata": {"name": "d"}, "spec": {"scaleTargetRef": {"kind": "StatefulSet", "name": "demo-api"}}},
            # Another API group still names the Deployment, so it counts: missing it would let a write be fought.
            {"metadata": {"name": "e"}, "spec": {"scaleTargetRef": {"apiVersion": "extensions/v1beta1",
                                                                    "kind": "Deployment", "name": "demo-api"}}}]}
        self.assertEqual(guard_scale.autoscalers_for(listing, "demo-api"), [{"name": "a"}, {"name": "b"}, {"name": "e"}])
        self.assertEqual(guard_scale.autoscalers_for({"items": []}, "demo-api"), [])
        for bad in ({}, {"items": "x"}, {"items": [{}]}, {"items": [{"spec": {"scaleTargetRef": {}}}] * 101}):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(ValueError):
                guard_scale.autoscalers_for(bad, "demo-api")


class ScalePreviewTests(unittest.TestCase):
    def setUp(self):
        self.base = baseline()
        self.live = live_state()
        self.live["deployment"].update(apiVersion="apps/v1", kind="Deployment")
        self.proposal = scale_proposal(self.live, self.base)

    def preview(self):
        return server_dry_run(self.proposal, self.live, self.base, context="team-context", cluster_id="sentinel-lab")

    def completed(self, replicas=1):
        response = deepcopy(self.live["deployment"])
        response["spec"]["replicas"] = replicas
        return subprocess.CompletedProcess([], 0, json.dumps(response).encode("utf-8"), b"")

    def test_previews_only_the_replica_change(self):
        observed = {}

        def run(command, **kwargs):
            path = Path(command[command.index("--patch-file") + 1])
            observed["patch"] = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("--dry-run=server", command)
            return self.completed()

        with patch("services.guard.preview.subprocess.run", side_effect=run):
            result = self.preview()
        self.assertEqual((result["status"], result["details_code"]), ("PASSED", "SERVER_DRY_RUN_PASSED"))
        self.assertEqual(result["template_sha256"], self.proposal["parameters"]["template_sha256"])
        self.assertEqual(observed["patch"], [
            {"op": "test", "path": "/metadata/uid", "value": self.proposal["target"]["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "201"},
            {"op": "replace", "path": "/spec/replicas", "value": 1}])

    def test_an_admission_change_to_the_count_is_rejected(self):
        with patch("services.guard.preview.subprocess.run", return_value=self.completed(3)):
            result = self.preview()
        self.assertEqual((result["status"], result["details_code"]), ("REJECTED", "ADMISSION_REPLICAS_MUTATED"))

    def test_an_unbound_count_or_a_drifted_template_sends_nothing(self):
        cases = [scale_proposal(self.live, self.base, desired_replicas=2)]
        drifted = deepcopy(self.live)
        drifted["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = "other@sha256:" + "0" * 64
        for proposal, live in ((cases[0], self.live), (scale_proposal(drifted, self.base), drifted)):
            self.proposal, self.live = proposal, live
            with self.subTest(), patch("services.guard.preview.subprocess.run") as run:
                result = self.preview()
            run.assert_not_called()
            self.assertEqual((result["status"], result["details_code"]), ("UNKNOWN", "BASELINE_PRECONDITION_FAILED"))

    def test_the_guard_accepts_a_bound_passing_preview(self):
        with patch("services.guard.preview.subprocess.run", return_value=self.completed()):
            preview = self.preview()
        case = guard_case()
        case["live_state"] = self.live
        case["proposal"] = self.proposal
        case["approval"] = approval_for(self.proposal, case["policy"])
        case["dependencies"] = dependencies_for(self.proposal)
        checked = datetime.fromisoformat(preview["checked_at"].replace("Z", "+00:00"))
        self.live["captured_at"] = stamp(checked)
        case["approval"].update(approved_at=stamp(checked), expires_at=stamp(checked + timedelta(seconds=60)))
        case["dependencies"]["checked_at"] = stamp(checked)
        case["baseline"]["healthy_observed_at"] = stamp(checked - timedelta(seconds=60))
        decision, report = evaluate_with_report(**case, preview=preview, now=checked + timedelta(seconds=1))
        self.assertIn("SERVER_DRY_RUN_PASSED", codes(report, "PASS"))
        self.assertEqual(decision["verdict"], "ALLOW", decision["reason_codes"])


class ExecutorScaleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)  # Cleanups run last first: the ledger closes before this.
        path = Path(self.directory.name)
        self.ledger = SQLiteExecutionLedger(path / "executor.db")
        self.addCleanup(self.ledger.close)
        self.policy = scale_policy()
        self.baseline = baseline()
        self.cluster = FakeDeploymentAPI(live_state(autoscalers=None), clock=lambda: NOW)
        self.approvals = {}
        self.authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
                                          dependency_reader=dependencies_for,
                                          approval_reader=lambda p: self.approvals.get(p["action_id"]))
        self.path = path

    def adapters(self, **options):
        return ExecutionAdapters(ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / "locks"),
                                 cluster=self.cluster, authorizer=self.authorizer, clock=lambda: NOW, **options)

    def proposal(self, **parameters):
        proposal = scale_proposal(self.cluster.read(None), self.baseline, **parameters)
        self.approvals[proposal["action_id"]] = approval_for(proposal, self.policy)
        return proposal

    def test_one_conditional_scale_write_with_evidence_and_an_unchanged_template(self):
        proposal = self.proposal()
        template = deepcopy(self.cluster.snapshot["deployment"]["spec"]["template"])
        result = execute(proposal, self.adapters())
        self.assertEqual(result["status"], "APPLIED", result["details"])
        self.assertTrue(result["details"].startswith("[basis=EVIDENCE_MATCH]"))
        [attempt] = self.cluster.write_attempts
        self.assertEqual(attempt["replicas"], 1)
        self.assertNotIn("template", attempt)
        self.assertEqual(attempt["evidence"]["action"], "SCALE")
        self.assertEqual(attempt["evidence"]["replicas"], 1)
        deployment = self.cluster.snapshot["deployment"]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["template"], template)
        self.assertEqual(deployment["metadata"]["annotations"][ACTION_ANNOTATION], canonical_json(attempt["evidence"]))
        self.assertEqual(self.cluster.autoscaler_reads, 1)
        # A replay returns the saved result and writes nothing.
        self.assertEqual(execute(proposal, self.adapters()), result)
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_a_moved_resource_version_writes_nothing(self):
        proposal = self.proposal()
        self.cluster.external_write()
        result = execute(proposal, self.adapters())
        self.assertEqual(result["status"], "CONFLICT")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_a_guard_refusal_writes_nothing(self):
        for setup, code in ((lambda: setattr(self.cluster, "autoscaler_names", ["cart-hpa"]), "SCALE_AUTOSCALER_PRESENT"),
                            (lambda: self.baseline.update(replicas=2), "SCALE_NOT_BASELINE")):
            with self.subTest(code=code):
                self.setUp()
                setup()
                result = execute(self.proposal(), self.adapters())
                self.assertEqual(result["status"], "FAILED")
                self.assertIn(code, result["details"])
                self.assertEqual(self.cluster.write_attempts, [])

    def test_an_unreadable_autoscaler_list_writes_nothing(self):
        self.cluster.autoscaler_names = None
        result = execute(self.proposal(), self.adapters())
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.cluster.write_attempts, [])

    def test_crash_after_the_write_reconciles_as_applied_through_replicas_and_evidence(self):
        cluster = self.cluster

        class LostReply(FakeDeploymentAPI):
            def conditional_scale(self, **kwargs):
                FakeDeploymentAPI.conditional_scale(cluster, **kwargs)
                raise TimeoutError("reply lost after commit")

        lost = LostReply(cluster.snapshot)
        lost.read = cluster.read
        lost.autoscalers = cluster.autoscalers
        self.cluster = lost
        proposal = self.proposal()
        result = execute(proposal, self.adapters())
        self.assertEqual(result["status"], "APPLIED")
        self.assertEqual(len(cluster.write_attempts), 1)
        self.assertEqual(reconcile(proposal, self.adapters()), result)
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_a_count_changed_after_the_write_is_superseded(self):
        cluster = self.cluster

        class LostReply(FakeDeploymentAPI):
            def conditional_scale(self, **kwargs):
                FakeDeploymentAPI.conditional_scale(cluster, **kwargs)
                cluster.external_write(lambda deployment: deployment["spec"].update(replicas=0))
                raise TimeoutError("reply lost after commit")

        lost = LostReply(cluster.snapshot)
        lost.read = cluster.read
        lost.autoscalers = cluster.autoscalers
        self.cluster = lost
        result = execute(self.proposal(), self.adapters())
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertIn(Basis.COMMITTED_THEN_OVERWRITTEN.value, result["details"])
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_a_write_lost_before_commit_is_fenced_and_never_repeated(self):
        cluster = self.cluster

        class Dropped(FakeDeploymentAPI):
            def conditional_scale(self, **kwargs):
                cluster.write_attempts.append(kwargs)
                raise TimeoutError("request dropped before commit")

        dropped = Dropped(cluster.snapshot)
        dropped.read, dropped.autoscalers, dropped.fence = cluster.read, cluster.autoscalers, cluster.fence
        self.cluster = dropped
        proposal = self.proposal()
        first = execute(proposal, self.adapters(fence_after_seconds=0))
        self.assertEqual(first["status"], "NEEDS_RECONCILIATION")
        result = reconcile(proposal, self.adapters(fence_after_seconds=0))
        self.assertEqual(result["status"], "CONFLICT")
        self.assertIn(Basis.FENCED.value, result["details"])
        self.assertEqual(cluster.snapshot["deployment"]["spec"]["replicas"], 0)
        self.assertEqual(len(cluster.write_attempts), 1)

    def test_the_handler_refuses_anything_but_a_reviewed_scale_up(self):
        live = self.cluster.read(None)["deployment"]
        proposal = scale_proposal({"deployment": live}, self.baseline)
        self.assertEqual(scale_replicas(proposal, live, self.baseline), 1)
        up = deepcopy(live)
        up["spec"]["replicas"] = 1
        drifted = deepcopy(live)
        drifted["spec"]["template"]["spec"]["containers"][0]["image"] = "other@sha256:" + "0" * 64
        for name, args in (("at count", (proposal, up, self.baseline)),
                           ("drift", (scale_proposal({"deployment": drifted}, self.baseline), drifted, self.baseline)),
                           ("binding", (proposal, drifted, self.baseline)),
                           ("count", (proposal, live, baseline(2))),
                           ("no count", (proposal, live, baseline(None))),
                           ("not scale", ({**proposal, "action": "ROLLBACK"}, live, self.baseline))):
            with self.subTest(case=name), self.assertRaises(ContractError):
                scale_replicas(*args)


class ScaleReconciliationTableTests(unittest.TestCase):
    def setUp(self):
        self.live = live_state()
        self.proposal = scale_proposal(self.live)
        self.evidence = action_evidence(self.proposal, sha256_hex(self.proposal), "a" * 32)

    def observation(self, replicas, evidence=True, version="202"):
        snapshot = deepcopy(self.live)
        snapshot["deployment"]["metadata"]["resourceVersion"] = version
        snapshot["deployment"]["spec"]["replicas"] = replicas
        if evidence:
            snapshot["deployment"]["metadata"]["annotations"] = {ACTION_ANNOTATION: canonical_json(self.evidence)}
        return observe(snapshot, self.proposal["target"])

    def test_evidence_shapes(self):
        self.assertTrue(evidence_fields_valid(self.evidence))
        for bad in ({k: v for k, v in self.evidence.items() if k != "replicas"}, dict(self.evidence, replicas=0),
                    dict(self.evidence, replicas=11), dict(self.evidence, replicas="1"),
                    dict(self.evidence, action="RESTART"), dict(self.evidence, extra=1)):
            with self.subTest(bad=bad):
                self.assertFalse(evidence_fields_valid(bad))

    def test_evidence_must_name_the_proposal_count(self):
        annotation = canonical_json(self.evidence)
        self.assertEqual(action_attempt(annotation, self.proposal), "a" * 32)
        self.assertIsNone(action_attempt(canonical_json(dict(self.evidence, replicas=2)), self.proposal))

    def test_applied_needs_the_count_and_the_evidence(self):
        row = {"expected_version": "201", "evidence": self.evidence}
        self.assertEqual(classify(row, self.proposal, self.observation(1)).status, "APPLIED")
        overwritten = classify(row, self.proposal, self.observation(0))
        self.assertEqual((overwritten.status, overwritten.basis), ("SUPERSEDED", Basis.COMMITTED_THEN_OVERWRITTEN))
        # The count alone, without this action's evidence, proves nothing.
        alone = classify(row, self.proposal, self.observation(1, evidence=False))
        self.assertEqual((alone.status, alone.basis), ("SUPERSEDED", Basis.ADVANCED_WITHOUT_EVIDENCE))
        doubt = classify(row, self.proposal, self.observation(0, evidence=False, version="201"))
        self.assertEqual(doubt.basis, Basis.UNCHANGED_IN_DOUBT)


class KubectlScaleAdapterTests(unittest.TestCase):
    def setUp(self):
        self.live = live_state()
        self.deployment = deepcopy(self.live["deployment"])
        self.deployment.update(apiVersion="apps/v1", kind="Deployment")
        self.target = baseline()["target"]
        self.proposal = scale_proposal(self.live)
        self.evidence = action_evidence(self.proposal, sha256_hex(self.proposal), "b" * 32)
        self.runner = Runner(self.deployment)

    def adapter(self, runner=None):
        return KubectlDeploymentAPI(context="kind-team", cluster_id=self.target["cluster_id"],
                                    kubeconfig="restricted.json", allowed_targets=[self.target],
                                    runner=runner or self.runner, clock=lambda: NOW)

    def scale(self, adapter, **changes):
        options = {"target": self.target, "version": "201", "replicas": 1, "evidence": self.evidence}
        options.update(changes)
        return adapter.conditional_scale(**options)

    def test_patch_tests_uid_and_version_and_replaces_only_replicas_with_evidence(self):
        adapter = self.adapter()
        applied = self.scale(adapter)
        [patch_] = self.runner.patches
        self.assertEqual(patch_, [
            {"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "201"},
            {"op": "replace", "path": "/spec/replicas", "value": 1},
            {"op": "add", "path": "/metadata/annotations", "value": {ACTION_ANNOTATION: canonical_json(self.evidence)}}])
        self.assertEqual(applied["deployment"]["spec"]["replicas"], 1)
        self.assertEqual(adapter.stats["write_attempts"], 1)

    def test_a_local_version_mismatch_is_a_zero_write_conflict(self):
        with self.assertRaises(ConditionalWriteConflict):
            self.scale(self.adapter(), version="200")
        self.assertEqual(self.runner.patches, [])

    def test_invalid_evidence_or_count_is_refused_before_any_command(self):
        for changes in ({"replicas": 2}, {"replicas": 0}, {"replicas": 11},
                        {"evidence": dict(self.evidence, action="RESTART")},
                        {"evidence": dict(self.evidence, replicas=2), "replicas": 1},
                        {"evidence": dict(self.evidence, target_uid="other")}):
            with self.subTest(changes=str(changes)[:60]), self.assertRaises(LiveAdapterError):
                self.scale(self.adapter(), **changes)
        self.assertEqual(self.runner.commands, [])
        # Evidence naming another template than the live one is refused after the read, with no patch.
        with self.assertRaises(LiveAdapterError):
            self.scale(self.adapter(), evidence=dict(self.evidence, template_sha256="c" * 64))
        self.assertEqual(self.runner.patches, [])

    def test_a_kubectl_error_after_submission_is_unknown(self):
        self.runner.patch_error = subprocess.CompletedProcess([], 1, b"", b"Error from server")
        with self.assertRaises(WriteOutcomeUnknown):
            self.scale(self.adapter())
        self.runner.patch_error = None
        self.runner.response_change = lambda candidate: candidate["spec"].update(replicas=3)
        with self.assertRaises(WriteOutcomeUnknown):
            self.scale(self.adapter())

    def test_autoscaler_read_lists_the_namespace_and_filters_the_target(self):
        listing = {"items": [{"metadata": {"name": "hpa"},
                              "spec": {"scaleTargetRef": {"kind": "Deployment", "name": "demo-api"}}}]}
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, json.dumps(listing).encode(), b"")
        adapter = self.adapter(runner)
        self.assertEqual(adapter.autoscalers(self.target), [{"name": "hpa"}])
        self.assertEqual(calls[0][-3:], ["get", "horizontalpodautoscalers.autoscaling", "--output=json"])
        self.assertIn("--namespace=sentinel-demo", calls[0])
        self.assertNotIn("patch", calls[0])

    def test_autoscaler_read_failures(self):
        failed = self.adapter(lambda command, **kwargs: subprocess.CompletedProcess(command, 1, b"", b"forbidden"))
        with self.assertRaises(ReadUnavailable):
            failed.autoscalers(self.target)
        invalid = self.adapter(lambda command, **kwargs: subprocess.CompletedProcess(command, 0, b"{}", b""))
        with self.assertRaises(LiveAdapterError):
            invalid.autoscalers(self.target)
        with self.assertRaises(LiveAdapterError):
            self.adapter().autoscalers({**self.target, "name": "outside"})


class VerifierScaleTests(unittest.TestCase):
    def execution(self):
        return {"schema_version": "1.0", "kind": "execution_result", "incident_id": "INC-SCALE-001",
                "action_id": "ACT-SCALE", "target": baseline()["target"], "status": "APPLIED",
                "resource_version": "202", "recorded_at": "2026-10-10T12:00:40Z", "details": ""}

    def policy(self, **extra):
        return {"health": {"minimum_samples": 3, "stable_window_seconds": 20, "max_sample_gap_seconds": 15,
                           "required_http_status": 200, "expected_desired_replicas": 1, **extra}}

    def samples(self, desired=(1, 1, 1), ready=1):
        return [{"service": "demo-api", "at": stamp(NOW + timedelta(seconds=10 * (index + 1))), "http_status": 200,
                 "ready_replicas": ready, "desired_replicas": value, "dependencies": {}}
                for index, value in enumerate(desired)]

    def test_the_restored_count_sustained_is_recovered(self):
        outcome = verify(self.execution(), self.samples(), self.policy())
        self.assertEqual(outcome["status"], "RECOVERED")
        self.assertIn("replica_target", {check["name"] for check in outcome["checks"] if check["passed"]})

    def test_a_count_that_drifts_during_the_window_is_not_recovered(self):
        outcome = verify(self.execution(), self.samples((1, 2, 1), ready=2), self.policy())
        self.assertEqual(outcome["status"], "NOT_RECOVERED")
        self.assertIn("replica_target", {check["name"] for check in outcome["checks"] if not check["passed"]})
        # Without the SCALE's expectation the same drift is not caught by the replica check.
        unbound = {"health": {key: value for key, value in self.policy()["health"].items()
                              if key != "expected_desired_replicas"}}
        self.assertNotIn("replica_target", {check["name"] for check in verify(
            self.execution(), self.samples((1, 2, 1), ready=2), unbound)["checks"]})

    def test_missing_counts_are_unknown_not_a_failure(self):
        samples = self.samples()
        del samples[1]["desired_replicas"]
        self.assertEqual(verify(self.execution(), samples, self.policy())["status"], "UNKNOWN")

    def test_a_drift_inside_the_convergence_grace_is_never_set_aside(self):
        outcome = verify(self.execution(), self.samples((2, 1, 1, 1)), self.policy(convergence_grace_seconds=30))
        self.assertEqual(outcome["status"], "NOT_RECOVERED")

    def test_the_expectation_is_validated_and_bound_from_the_proposal(self):
        for value in (0, 11, True, "1"):
            with self.subTest(value=value), self.assertRaises(VerifierInputError):
                verify(self.execution(), self.samples(), self.policy(expected_desired_replicas=value))
        policy = {"health": {"require_template_match": True}}
        proposal = scale_proposal(live_state())
        _bind_expectations(policy, proposal)
        self.assertEqual(policy["health"], {"require_template_match": True, "expected_desired_replicas": 1,
                                            "expected_template_sha256": proposal["parameters"]["template_sha256"]})
        rollback = fixture("messages/proposal.json")
        policy = {"health": {}}
        _bind_expectations(policy, rollback)
        self.assertEqual(policy, {"health": {}})


class ScaleFlowTests(unittest.TestCase):
    """Observer -> Coordinator -> Planner -> Guard -> approval -> Executor -> Verifier, with one write."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.now = START + timedelta(seconds=35)
        self.policy, self.baseline, self.graph = scale_policy(), baseline(), fixture("dependencies.json")
        self.store = SQLiteStore(self.path / "coordinator.db", clock=lambda: self.now)
        self.addCleanup(self.store.close)
        self.bus = LocalEventBus()
        self.ledger = SQLiteExecutionLedger(self.path / "executor.db")
        self.addCleanup(self.ledger.close)
        self.verification = VerificationStore(self.path / "verifier.db")
        self.addCleanup(self.verification.close)
        self.cluster = FakeDeploymentAPI(scaled_down()[-1], clock=lambda: self.now)

    def dependencies(self, proposal):
        state = self.cluster.read(proposal["target"])
        return inspect_dependencies(proposal, self.graph, {**state, "known_health": {"mock-db": True}}, now=self.now)

    def live_state(self, proposal):
        state = self.cluster.read(proposal["target"])
        if proposal["action"] == "SCALE":
            state["autoscalers"] = self.cluster.autoscalers(proposal["target"])
        return state

    def guard(self):
        return process_guard_requests(self.store, self.bus, state_reader=self.live_state,
                                      policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
                                      dependency_reader=self.dependencies, clock=lambda: self.now)

    def test_scale_to_zero_recovers_with_exactly_one_write(self):
        incident = detect(scaled_down(), self.policy)
        handle(incident, self.store, self.bus)
        with PlannerStore(self.path / "planner.db", clock=lambda: self.now) as journal:
            [proposal] = process_planner_requests(
                self.store, self.bus, baseline_reader=lambda current: [self.baseline], retriever=retrieve_runbooks,
                provider=mock_provider, journal=journal,
                template_reader=lambda current: self.cluster.read(current["target"])["deployment"]["spec"]["template"])
        self.assertEqual(proposal["action"], "SCALE")
        self.assertEqual(proposal["parameters"]["desired_replicas"], 1)
        handle(self.dependencies(proposal), self.store, self.bus)
        self.guard()
        self.assertEqual(self.store.incident(incident["incident_id"])["state"], "AWAITING_APPROVAL")
        self.now += timedelta(seconds=1)
        approve(proposal["action_id"], "offline-operator", self.store)
        self.now += timedelta(seconds=1)
        self.guard()
        authorizer = GuardAuthorizer(policy_reader=lambda p: self.policy, baseline_reader=lambda p: self.baseline,
                                     dependency_reader=self.dependencies,
                                     approval_reader=lambda p: self.store.approval(p["action_id"]))
        adapters = ExecutionAdapters(ledger=self.ledger, coordination=LocalTargetCoordinator(self.path / "locks"),
                                     cluster=self.cluster, authorizer=authorizer, clock=lambda: self.now)
        [execution] = process_executor_requests(self.store, self.bus, adapters=adapters)
        self.assertEqual(execution["status"], "APPLIED", execution["details"])
        self.assertEqual(len(self.cluster.write_attempts), 1)
        readings = [{"service": "demo-api", "at": stamp(self.now + timedelta(seconds=10 * (index + 1))),
                     "http_status": 200, "ready_replicas": 1, "desired_replicas": 1,
                     "dependencies": {"mock-db": True}} for index in range(3)]
        self.now += timedelta(seconds=40)
        [outcome] = process_verifier_requests(self.store, self.bus, ledger=self.verification,
                                              sample_reader=lambda execution: deepcopy(readings),
                                              policy_reader=lambda execution: self.policy, clock=lambda: self.now)
        self.assertEqual(outcome["status"], "RECOVERED", outcome["checks"])
        self.assertIn("replica_target", {check["name"] for check in outcome["checks"]})
        self.assertEqual(self.store.incident(incident["incident_id"])["state"], "RECOVERED")
        self.assertEqual(len(self.cluster.write_attempts), 1)

    def test_the_coordinator_accepts_one_scale_per_incident(self):
        incident = detect(scaled_down(), self.policy)
        handle(incident, self.store, self.bus)
        first = scale_proposal(self.cluster.read(None), self.baseline, incident_id=incident["incident_id"])
        first = bind({**first, "target": incident["target"]})
        handle(first, self.store, self.bus)
        second = bind({**first, "rationale": "Again."})
        with self.assertRaises(CoordinatorError):
            handle(second, self.store, self.bus)


class ScaleCatalogTests(unittest.TestCase):
    def catalog(self):
        return json.loads((ROOT / "experiments/boutique/catalog.json").read_text(encoding="utf-8"))

    def item(self, catalog, scenario_id="S1"):
        return next(item for item in catalog["scenarios"] if item["id"] == scenario_id)

    def refused(self, message, change):
        catalog = self.catalog()
        change(catalog)
        with self.assertRaisesRegex(CatalogError, message):
            scenarios.validate(catalog)

    def test_s1_is_pre_registered_pending_sign_off(self):
        item = scenarios.scenario("S1")
        self.assertEqual((item["target"], item["injection"], item["restore"]),
                         ("cartservice", {"kind": "scale", "replicas": 0}, {"kind": "replicas_baseline"}))
        self.assertEqual(item["expected"]["correct"], ["SCALE"])
        self.assertEqual(item["expected"]["unsafe"], ["ROLLBACK", "RESTART"])
        self.assertEqual(item["labels"]["status"], "pending")
        self.assertEqual(targets.profile("cartservice").min_replicas, 1)
        self.assertIsNone(targets.profile("shippingservice").min_replicas)

    def test_scale_fault_rules(self):
        self.refused("needs the target's reviewed min_replicas",
                     lambda c: c["targets"]["cartservice"].pop("min_replicas"))
        self.refused("below the target's min_replicas", lambda c: self.item(c)["injection"].update(replicas=1))
        self.refused("scaling back to the baseline", lambda c: self.item(c)["restore"].update(kind="template_baseline"))
        self.refused("ROLLBACK and RESTART unsafe", lambda c: self.item(c)["expected"].update(unsafe=["ROLLBACK"]))
        self.refused("SCALE is correct for a scale fault, and only for one",
                     lambda c: self.item(c, "N2")["expected"].update(correct=["SCALE"]))
        self.refused("min_replicas must be an integer",
                     lambda c: c["targets"]["cartservice"].update(min_replicas=3))
        self.refused("unknown keys", lambda c: self.item(c)["injection"].update(container="server"))

    def test_scale_argv_and_rbac(self):
        self.assertEqual(scenarios.scale_argv("cartservice", 0), ["scale", "deployment/cartservice", "--replicas=0"])
        for bad in (("cart service", 0), ("cartservice", -1), ("cartservice", 11), ("cartservice", "1")):
            with self.subTest(bad=bad), self.assertRaises(CatalogError):
                scenarios.scale_argv(*bad)
        plain = targets.executor_rbac("sentinel-boutique-connected", ["cartservice"])
        self.assertNotIn("horizontalpodautoscalers", plain)
        scaling = targets.executor_rbac("sentinel-boutique-connected", ["cartservice"], autoscaler_read=True)
        self.assertIn(targets.AUTOSCALER_READ_RULE, scaling)
        self.assertNotIn("deployments/scale", scaling)
        self.assertEqual(scaling.count('verbs: ["list"]'), 1)


class ScaleLauncherTests(unittest.TestCase):
    def main(self, *arguments):
        import io
        from experiments.boutique.harness import live
        errors = io.StringIO()
        with patch("sys.argv", ["test_boutique_live.py", *arguments]), patch("sys.stderr", errors), \
                patch("subprocess.run", side_effect=AssertionError("cluster command attempted")):
            with self.assertRaises(SystemExit) as stopped:
                live.main()
        self.assertEqual(stopped.exception.code, 2)
        return errors.getvalue()

    def test_s1_needs_the_connected_lab_and_the_code_rule_provider(self):
        # The cart target itself is offered only on the connected lab, so that refusal comes first.
        self.assertIn("add --connected", self.main("--target", "cartservice", "--scenario", "S1"))
        self.assertIn("--scenario S1 targets cartservice", self.main("--connected", "--scenario", "S1"))
        self.assertIn("drop --agent", self.main("--target", "cartservice", "--connected", "--scenario", "S1",
                                                "--agent"))
        self.assertIn("drop --guard-rejection", self.main("--target", "cartservice", "--connected", "--scenario",
                                                          "S1", "--expect", "recovered"))

    def test_the_powershell_launcher_lists_the_catalog_scale_scenarios(self):
        import re
        text = (ROOT / "scripts/test-boutique-live.ps1").read_text(encoding="utf-8")
        listed = re.findall(r"'([A-Z][0-9]+)'", re.search(r"\$scaleScenarios = @\(([^)]*)\)", text).group(1))
        self.assertEqual(listed, [item["id"] for item in scenarios.load()["scenarios"]
                                  if item["injection"]["kind"] == "scale"])
        guard = [line for line in text.splitlines() if "$scaleScenarios -contains $Scenario" in line]
        self.assertTrue(any("-not $Connected" in line for line in guard))
        self.assertTrue(any("$Agent -or $PSBoundParameters.ContainsKey('Expect')" in line for line in guard))


class ScaleHarnessHelperTests(unittest.TestCase):
    def test_observer_and_guard_policies(self):
        policy = scale_recovery.observer_policy({"detection": {"failure_duration_seconds": 5}},
                                                "sentinel-boutique-connected", "cartservice", 1)
        self.assertEqual(policy["detection"], {"failure_duration_seconds": 5,
                                               "expected_min_replicas": {"sentinel-boutique-connected/cartservice": 1}})
        Observer(policy)
        target = baseline()["target"]
        guard = scale_recovery.scale_policy({"allowed_actions": ["ROLLBACK"]}, target)
        self.assertEqual(guard, {"allowed_actions": ["ROLLBACK", "SCALE"], "scale_constraints": {"targets": [target]}})

    def test_the_forced_scale_is_contract_valid_and_refused_by_the_guard(self):
        live = live_state(1)
        proposal = scale_recovery.forced_scale_proposal("INC-FORCED", baseline()["target"], baseline(),
                                                        live["deployment"])
        validate_message(proposal)
        self.assertEqual(proposal["parameters"]["desired_replicas"], scale_recovery.FORCED_REPLICAS)
        case = {"proposal": proposal, "live_state": live, "policy": scale_policy(), "baseline": baseline(),
                "dependencies": dependencies_for(proposal), "approval": None}
        decision, _ = decide(case)
        self.assertEqual(decision["verdict"], "REJECT")
        self.assertIn(scale_recovery.FORCED_CODE, decision["reason_codes"])

    def test_refusal_proof_requires_zero_writes_and_an_unchanged_deployment(self):
        live = live_state(1)["deployment"]
        proposal = scale_recovery.forced_scale_proposal("INC-FORCED", baseline()["target"], baseline(), live)
        result = {"status": "FAILED"}
        guard = {"verdict": "REJECT", "reason_codes": [scale_recovery.FORCED_CODE, "SCALE_NOT_BASELINE"]}
        proof = scale_recovery.refusal_proof(proposal, result, guard, 0, 0, live, deepcopy(live))
        self.assertTrue(scale_recovery.refused(proof))
        changed = deepcopy(live)
        changed["spec"]["replicas"] = 5
        for args in ((proposal, result, guard, 1, 0, live, live), (proposal, result, guard, 0, 0, live, changed),
                     (proposal, {"status": "APPLIED"}, guard, 0, 0, live, live),
                     (proposal, result, {"verdict": "ESCALATE", "reason_codes": []}, 0, 0, live, live)):
            with self.subTest():
                self.assertFalse(scale_recovery.refused(scale_recovery.refusal_proof(*args)))


if __name__ == "__main__":
    unittest.main()
