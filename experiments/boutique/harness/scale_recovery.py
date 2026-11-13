"""A scale-down fault wired into guarded recovery on the connected Boutique lab (P5.07).

Catalog scenario S1 scales cartservice to 0 replicas, below its reviewed
minimum (the catalog target's min_replicas). The template stays at the
baseline, so ROLLBACK and RESTART change nothing; SCALE back to the baseline's
own replica count is the repair. Written and tested offline only; it has not
run against a cluster (docs/testing.md#scale-recovery-live-recipe).

- Detection. The Observer's policy carries the reviewed minimum
  (expected_min_replicas) from the start of the run, so its checkpoint is bound
  to one policy, and a count below it raises ReplicasBelowReviewedMinimum.
- Baseline. The measured baseline also records the Deployment's spec.replicas,
  the count a SCALE restores.
- Planning. The code rule provider (mock_provider) plans with a trusted read of
  the live template: the signal plus an unchanged configuration give SCALE, and
  the Planner binds the baseline's count and the live template's hash.
- Execution. The Guard policy allowlists the target for SCALE
  (scale_constraints) and the run's Executor Role may also list
  HorizontalPodAutoscalers in its namespace (read only), so the Guard can see
  that none would fight the write. The Executor sends one conditional patch of
  spec.replicas through the main deployments resource; it is never granted the
  deployments/scale subresource.
- Forced refusal. After the verified recovery, a harness-forced SCALE to 5
  goes to the restricted Executor with no approval. Its fresh Guard check must
  REJECT it with WORKLOAD_REPLICAS_EXCEED_POLICY (and SCALE_NOT_BASELINE), with
  zero write attempts and the Deployment unchanged.
- Cleanup. emergency_restore() scales back to the baseline's count as the lab
  administrator; that is never counted as Sentinel's recovery.
"""

from copy import deepcopy
from uuid import uuid4

from experiments.boutique.harness import scenarios, targets
from experiments.boutique.harness.durable_case import run_durable_case
from experiments.boutique.harness.scoring import ScenarioScoringMixin
from experiments.recovery.live import require, save, utc
from services.executor.core import execute
from services.executor.store import fingerprint
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from shared import log
from shared.actions import SCALE_MAX_REPLICAS

LOG = log.get_logger("experiment")

PLANNING_FOLDER = "scale-recovery"
# The forced wrong SCALE: above every catalog target's max_replicas ceiling (at most 2 today) and within the contract.
FORCED_REPLICAS = 5
FORCED_CODE = "WORKLOAD_REPLICAS_EXCEED_POLICY"


def observer_policy(base, namespace, name, minimum):
    """The Observer policy with the reviewed minimum replica count of one target added."""
    policy = deepcopy(base)
    policy.setdefault("detection", {})["expected_min_replicas"] = {namespace + "/" + name: minimum}
    return policy


def scale_policy(policy, target):
    """The Guard policy of a scale run: SCALE allowed for this target alone, beside ROLLBACK."""
    result = deepcopy(policy)
    result["allowed_actions"] = ["ROLLBACK", "SCALE"]
    result["scale_constraints"] = {"targets": [deepcopy(target)]}
    return result


def forced_scale_proposal(incident_id, target, baseline, deployment, replicas=FORCED_REPLICAS):
    """A harness-forced SCALE to replicas, bound to the baseline and the live template like a real one."""
    require(type(replicas) is int and 1 <= replicas <= SCALE_MAX_REPLICAS, "Forced replicas must fit the contract")
    health = (baseline.get("health_evidence") or {}).get("evidence_id")
    return {"schema_version": "1.0", "kind": "proposal", "incident_id": incident_id,
            "action_id": "ACT-FORCED-" + uuid4().hex, "target": deepcopy(target), "action": "SCALE",
            "parameters": {"baseline_id": baseline["baseline_id"], "desired_replicas": replicas,
                           "template_sha256": fingerprint(deployment["spec"]["template"])},
            "based_on_resource_version": deployment["metadata"]["resourceVersion"],
            "rationale": "Harness-forced wrong scale for the negative check; the Guard must refuse it.",
            "evidence_refs": [incident_id, *([health] if health else [])]}


def refusal_proof(proposal, result, guard, attempts, journal_attempts, before, after):
    """The zero-write proof of a forced refusal, from the Executor's own records and two reads."""
    return {"action": "SCALE", "desired_replicas": proposal["parameters"]["desired_replicas"],
            "expected_reason": FORCED_CODE, "action_id": proposal["action_id"],
            "executor_status": result["status"], "guard_verdict": guard.get("verdict"),
            "guard_reason_codes": guard.get("reason_codes", []), "executor_patch_attempts": attempts,
            "executor_journal_attempts": journal_attempts,
            "replicas_unchanged": before["spec"].get("replicas") == after["spec"].get("replicas"),
            "template_unchanged": fingerprint(before["spec"]["template"]) == fingerprint(after["spec"]["template"]),
            "generation_unchanged": before["metadata"]["generation"] == after["metadata"]["generation"]}


def refused(proof):
    """True when the forced SCALE was refused by the Guard with the expected code and nothing was written."""
    return (proof["executor_status"] == "FAILED" and proof["guard_verdict"] == "REJECT"
            and FORCED_CODE in proof["guard_reason_codes"] and proof["executor_patch_attempts"] == 0
            and proof["executor_journal_attempts"] == 0 and proof["replicas_unchanged"]
            and proof["template_unchanged"] and proof["generation_unchanged"])


def scale_pass_line(scenario_id, summary):
    graded = summary.get("scenario_result") or {}
    refusals = len(summary.get("forced_refusals", []))
    return (f"PASS: scale scenario {scenario_id} decided {graded.get('actual')} (scored {graded.get('score')}); "
            f"{refusals} forced SCALE refused with zero writes")


class ScaleRecoveryMixin(ScenarioScoringMixin):
    """Run one catalog scale fault through Observer, Planner (code rule), Guard, Executor and Verifier."""

    expect = "recovered"

    @property
    def minimum_replicas(self):
        minimum = self.scenario["target_profile"].get("min_replicas")
        require(type(minimum) is int and minimum >= 1, "The scale scenario's target has no reviewed min_replicas")
        return minimum

    def observer_policy(self):
        return observer_policy(super().observer_policy(), self.namespace, self.name, self.minimum_replicas)

    def measured_baseline(self, template, samples):
        baseline = super().measured_baseline(template, samples)
        replicas = self.deployment()["spec"].get("replicas", 1)
        require(type(replicas) is int and self.minimum_replicas <= replicas <= SCALE_MAX_REPLICAS,
                "The healthy baseline's replica count must be at least the reviewed minimum")
        baseline["replicas"] = replicas
        return baseline

    def write_executor_rbac(self):
        self.rbac_path = self.directory / "executor-rbac.yaml"
        self.rbac_path.write_text(targets.executor_rbac(self.namespace, self.executor_names(), autoscaler_read=True),
                                  encoding="utf-8")

    def prepare(self):
        item = self.scenario
        require(item["injection"]["kind"] == "scale", "The scale harness runs only catalog scale scenarios")
        scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context, namespace=self.namespace)
        super().prepare()
        self.policy = scale_policy(self.policy, self.target)
        save(self.directory / "policy.json", self.policy)

    def permissions(self):
        super().permissions()
        result = self.kubectl("auth", "can-i", "list", "horizontalpodautoscalers.autoscaling", restricted=True,
                              allow_failure=True)
        require(result.stdout.strip() == "yes", "The scale run's Executor cannot list HorizontalPodAutoscalers")
        self.check("restricted Executor may list HorizontalPodAutoscalers (read only) for the SCALE check")

    def inject_fault(self):
        item = self.scenario
        if item["injection"]["kind"] != "scale":
            return super().inject_fault()
        scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context, namespace=self.namespace)
        require(item["target"] == self.name, "The scenario targets another workload")
        replicas = item["injection"]["replicas"]
        require(self.baseline is not None and replicas < self.baseline["replicas"],
                "A scale fault must go below the baseline's replica count")
        self.kubectl(*scenarios.scale_argv(self.name, replicas))
        self.summary.setdefault("fault_injected_at", utc())
        self.event(1, "fault", f"Operator scaled {self.name} to {replicas} replicas, below its reviewed minimum "
                   f"{self.minimum_replicas}; the template is unchanged", target=self.target)
        return None

    def emergency_restore(self):
        """Scale back to the baseline's count as the administrator; never counted as Sentinel recovery."""
        restored = super().emergency_restore()
        if self.baseline is None or "replicas" not in self.baseline:
            return restored
        try:
            if self.deployment()["spec"].get("replicas") != self.baseline["replicas"]:
                self.kubectl(*scenarios.scale_argv(self.name, self.baseline["replicas"]))
            self.summary["replicas_restored_by_administrator"] = True
        except Exception as error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "boutique.scale_restore.failed", error, level=log.ERROR)
            self.summary["replicas_restored_by_administrator"] = False
        return restored

    def approve_rule(self, proposal, decision):
        return self.request_approval(proposal, decision)

    def recover_incident(self, incident):
        require(incident is not None, "The scale fault raised no incident")
        folder = self.directory / PLANNING_FOLDER
        planning = {"baseline_reader": lambda current: [self.baseline], "retriever": retrieve_runbooks,
                    "provider": mock_provider,
                    "template_reader": lambda current: self.deployment()["spec"]["template"]}
        self.summary.update(planning="Code rule provider with a trusted live-template read; no model call",
                            expected_outcome=self.expect)
        try:
            state = run_durable_case(self, incident, folder=folder, planning=planning, approve=self.approve_rule)
        except Exception:
            self.score_rule(incident, folder)  # A failure after planning still scores the delivered decision.
            raise
        graded = self.score_rule(incident, folder, state)
        require(graded is not None, "Rule planning delivered no proposal")
        require(state == "RECOVERED", f"Scale recovery stopped at {state}; expected RECOVERED")
        require(graded["matched_expected"], f"Scenario {graded['scenario']} scored {graded['score']}: decision "
                f"{graded['actual']}; pre-registered correct {graded['expected']['correct']}")
        require(self.deployment()["spec"].get("replicas") == self.baseline["replicas"],
                "The Deployment does not hold the baseline's replica count after recovery")
        self.check(f"Scenario {graded['scenario']} decided {graded['actual']} by the code rule; Coordinator {state}",
                   action_id=graded.get("action_id"))

    def score_rule(self, incident, folder, state=None):
        if not (folder / "planner.db").is_file():
            return None
        with PlannerStore(folder / "planner.db") as journal:
            record = journal.get(incident["incident_id"])
        if not record or not record["delivered"] or not record["proposal"]:
            return None
        proposal = record["proposal"]
        state = state or (self.store.incident(incident["incident_id"]) or {}).get("state")
        facts = self.scenario_facts(incident, state, proposal["action_id"], folder)
        facts.update(planning_error_code=record["error_code"], decided_by="rule", action_id=proposal["action_id"])
        outcome = scenarios.decision(proposal, record["error_code"])
        source = scenarios.decision_source(proposal, record["error_code"], decided_by="rule")
        return self.save_scenario_result(scenarios.result(self.scenario, outcome, source=source, **facts))

    def after_recovery_scenarios(self):
        self.forced_refusal()

    def forced_refusal(self):
        """The restricted Executor must refuse a forced SCALE to FORCED_REPLICAS with zero writes and no approval."""
        folder = self.directory / "forced-scale"
        folder.mkdir()
        incident_id = "INC-FORCED-SCALE-" + uuid4().hex[:12].upper()
        before = self.deployment()
        proposal = forced_scale_proposal(incident_id, self.target, self.baseline, before)
        for name, value in (("proposal.json", proposal), ("dependencies.json", self.dependency_result(proposal)),
                            ("approval.json", None)):
            save(folder / name, value)
        adapters = self.adapters(folder)
        try:
            started = adapters.cluster.stats["write_attempts"]
            result = execute(proposal, adapters)
            entry = adapters.ledger.get(proposal["action_id"]) or {}
            attempts = adapters.cluster.stats["write_attempts"] - started
        finally:
            adapters.ledger.close()
        after = self.deployment()
        proof = refusal_proof(proposal, result, entry.get("guard_result") or {}, attempts, entry.get("attempts"),
                              before, after)
        save(folder / "proof.json", proof)
        self.summary.setdefault("forced_refusals", []).append(proof)
        require(refused(proof), f"Forced SCALE to {FORCED_REPLICAS} was not refused with {FORCED_CODE} and zero "
                f"writes: {proof['guard_verdict']} {proof['guard_reason_codes']}")
        self.check(f"Forced SCALE to {FORCED_REPLICAS} refused by the Guard with {FORCED_CODE}; zero Executor writes",
                   action_id=proposal["action_id"])
        return proof
