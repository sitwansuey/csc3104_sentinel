"""Two simultaneous faults on the connected Boutique, verified per target (P4.05, issue #77).

A catalog pair (experiments/boutique/catalog.json, "pairs") names two scenarios
on two different targets that are injected together:

- an independent pair (shipping-config+cart-pod: R1 and C3), where neither
  target calls the other, so each incident is planned, checked, repaired and
  verified on its own; and
- a dependency-ordered pair (redis-pod+cart-config: D1 and C1), where cart
  calls redis-cart, so the cart repair must wait for Redis. The Guard's
  dependency check counts callers as well as callees, and C1 leaves the cart
  NotReady, so with the rule as it stands the Redis repair is also refused
  and the expected live result is DEPENDENCY_DEADLOCK, a safe stop that shows
  no ordering (docs/dual-fault.md, L-CALLER-BLOCKS-CALLEE-REPAIR).

Each target is verified by its own probe and its own callees only
(connected.verification_dependencies, the "target" verification scope). The
whole storefront is judged separately, after both incidents settle, as a
ShopOutcome (shop_outcome): RECOVERED only when both incidents are RECOVERED
and the storefront then passes three journeys in a row, PARTIAL when one
recovered and the other did not, FAILED when none recovered or the storefront
still fails, and UNKNOWN when an incident has not settled or the storefront
evidence is incomplete. A broken service therefore can no longer mark another
target's correct repair NOT_RECOVERED, and the shop's result never changes an
incident's state.

PairRunner advances both incidents through the real Planner, Guard, Executor
and Verifier workers until both settle or a bounded budget ends. A repair the
Guard refuses with DEPENDENCY_BLOCKED, where every blocker is the other target
of the pair, waits: once that target is RECOVERED the incident is planned
again (at most once; the Coordinator accepts a new proposal after REJECTED)
and its fresh dependency finding is checked again. When the two refusals name
each other, the run stops with DEPENDENCY_DEADLOCK and no write, for a person
to decide; there is no retry loop. Any other refusal, an escalation, a refused
approval or a refused re-plan stops that incident.

The pure functions and PairRunner run offline in tests/test_overlapping_recovery.py
and tests/test_dependency_revalidation.py. PairBoutiqueExperiment wires them
to the connected lab for python scripts/test_boutique_live.py --connected
--pair <id>; the live recipe is in docs/dual-fault.md and has not been run.
"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from experiments import lab_lock
from experiments.boutique.harness import chaos, scenarios, targets
from experiments.boutique.harness.chaos_recovery import RESTART_BUDGET, ChaosRecoveryMixin, preflight
from experiments.boutique.harness.connected import ConnectedBoutiqueExperiment
from experiments.boutique.harness.live import PROBE_PROTO_CONFIGMAP, ROOT
from experiments.recovery.live import Experiment, require, save
from experiments.recovery.live import utc as live_utc
from runtime.approval_session import LocalApprovalAuthority, MODES, approver_for, operator_identity
from runtime.executor_pipeline import process_executor_requests
from runtime.guard_pipeline import process_guard_requests
from runtime.live_executor import build_adapters
from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore
from runtime.planner_pipeline import process_planner_requests
from runtime.recovery_runner import advance as advance_incident
from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import CoordinatorError, handle
from services.executor.core import execute
from services.executor.store import ActionConflict, SQLiteExecutionLedger, fingerprint
from services.observer.kubernetes import CollectionError, KubernetesEvidenceCollector, KubectlReader
from services.planner.core import mock_provider, propose, retrieve_runbooks
from services.verifier.store import VerificationStore
from shared import log
from shared.actions import config_fingerprint

LOG = log.get_logger("experiment")

DEFAULT_BUDGET_SECONDS = 900
POLL_SECONDS = 2
MAX_REPLANS = 1
SHOP_PASSES = 3
SHOP_INTERVAL_SECONDS = 5
# Both faults must be applied within this many seconds of each other (#77).
INJECTION_SPREAD_SECONDS = 5
DETECTION_SECONDS = 180
# Coordinator states after which the runner never acts on an incident again.
SETTLED = frozenset({"RECOVERED", "NEEDS_REVIEW", "FAILED"})
STOP_REASONS = ("REJECTED", "ESCALATED", "BLOCKER_NOT_RECOVERED", "REPLAN_BUDGET_EXHAUSTED", "REPLAN_REFUSED",
                "DEPENDENCY_DEADLOCK", "APPROVAL_REFUSED")
PAIR_POLICY_VERSION = "executor-live-pair-1"


class ApprovalRefused(Exception):
    """Raised by a pair run's approve callback when the operator refuses; the runner stops that incident."""


def utc(value=None):
    return (value or datetime.now(UTC)).isoformat().replace("+00:00", "Z")


def pair_order(pair):
    """The pair's target names in the order the runner acts on them: the dependency first."""
    names = [item["target"] for item in pair["items"]]
    if pair["kind"] == "dependency_ordered":
        return [pair["order"]["first"], pair["order"]["then"]]
    return names


# The separate shop aggregate (pure).

def shop_outcome(states, journeys, *, stopped=(), required_passes=SHOP_PASSES):
    """The whole-storefront result after both incidents settled; separate from every incident outcome.

    states maps each pair target to its Coordinator incident state (None when
    no incident was recorded). journeys are storefront journeys collected
    after both settled, oldest first, each with "passed" True, False or None.
    stopped names the targets PairRunner stopped (an escalation, a refused
    approval, a deadlock): the runner never acts on them again, so they count
    as settled and not recovered, whatever state the Coordinator still shows.

    UNKNOWN when an incident has not settled. Then PARTIAL when some but not
    all incidents are RECOVERED, and FAILED when none is: the storefront
    cannot change either answer. When every incident is RECOVERED, the
    storefront decides: UNKNOWN when its evidence is missing or incomplete
    (fewer than required_passes journeys, or one whose result is not known),
    RECOVERED when the last required_passes journeys all passed, FAILED when
    one failed. The result is evidence only: it never changes an incident's
    state.
    """
    if not isinstance(states, dict) or not states or type(required_passes) is not int or required_passes < 1:
        raise ValueError("shop_outcome needs the pair's incident states and a positive pass count")
    recovered = sorted(name for name, state in states.items() if state == "RECOVERED")
    others = sorted(name for name in states if name not in recovered)
    passed = [item.get("passed") if isinstance(item, dict) else None for item in journeys or []]
    complete = len(passed) >= required_passes and all(type(value) is bool for value in passed)
    tail = passed[-required_passes:]
    storefront = {"journeys": len(passed), "passed": sum(value is True for value in passed),
                  "required_passes": required_passes, "complete": complete,
                  "sustained": complete and all(tail)}
    unsettled = sorted(name for name, state in states.items()
                       if state not in SETTLED | {"REJECTED"} and name not in stopped)
    if unsettled:
        status, reason = "UNKNOWN", "incident not settled: " + ", ".join(unsettled)
    elif others:
        status, reason = (("PARTIAL", "not recovered: " + ", ".join(others)) if recovered else
                          ("FAILED", "no target recovered"))
    elif not complete:
        status, reason = "UNKNOWN", "every target recovered but the storefront evidence is missing or incomplete"
    elif storefront["sustained"]:
        status, reason = "RECOVERED", ("every target recovered and the storefront passed "
                                       + str(required_passes) + " journeys in a row")
    else:
        status, reason = "FAILED", "every target recovered but the storefront still fails"
    return {"status": status, "reason": reason, "incidents": dict(states), "recovered": recovered,
            "not_recovered": others, "storefront": storefront}


# Evidence read from the Coordinator and the Executor ledger (pure).

def writes_by_target(ledger_db):
    """Executor write attempts per target name, from an Executor ledger's sqlite3 connection."""
    counts = {}
    for proposal, attempts in ledger_db.execute("SELECT proposal, attempts FROM executor_actions"):
        name = json.loads(proposal)["target"]["name"]
        counts[name] = counts.get(name, 0) + int(attempts)
    return counts


def ordering_proof(transitions, *, first_incident, then_incident):
    """Whether the second target's repair was allowed only after the first RECOVERED, by Coordinator sequence.

    transitions is the Coordinator's lifecycle journal (store.transitions()).
    Every READY (a Guard ALLOW) of then_incident must come after
    first_incident's RECOVERED, and there must be one.
    """
    recovered = [row for row in transitions if row["incident_id"] == first_incident and row["to_state"] == "RECOVERED"]
    ready = [row for row in transitions if row["incident_id"] == then_incident and row["to_state"] == "READY"]
    first = recovered[0]["seq"] if recovered else None
    early = [row["seq"] for row in ready if first is None or row["seq"] < first]
    return {"passed": first is not None and bool(ready) and not early,
            "first_recovered": {"seq": first, "at": recovered[0]["recorded_at"]} if recovered else None,
            "then_ready": [{"seq": row["seq"], "at": row["recorded_at"]} for row in ready],
            "then_ready_before_first_recovered": early}


def cross_incident_transitions(store):
    """Transitions of one incident caused by another incident's message or approval; [] when there are none."""
    violations = []
    for row in store.transitions():
        payload = store.message_payload(row["cause"]) if row["cause"] else None
        if payload is not None:
            message = json.loads(payload)
            owner, action_id = message.get("incident_id"), message.get("action_id")
        else:
            action = store.action(row["action_id"]) if row["action_id"] else None
            owner, action_id = (action["incident_id"] if action else row["incident_id"]), row["action_id"]
        if owner != row["incident_id"] or (action_id and row["action_id"] and action_id != row["action_id"]):
            violations.append({"seq": row["seq"], "incident_id": row["incident_id"], "action_id": row["action_id"],
                               "cause_incident_id": owner, "cause_action_id": action_id})
    return violations


def incident_timings(transitions, incidents):
    """Per target: when its incident opened, first became READY and RECOVERED, from the Coordinator journal.

    incidents maps target names to incident IDs. Seconds are measured on the
    Coordinator's own clock, so they compare with single-fault runs' journals.
    """
    def stamp(value):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    result = {}
    for name, incident_id in incidents.items():
        rows = [row for row in transitions if row["incident_id"] == incident_id]
        first = {state: next((row["recorded_at"] for row in rows if row["to_state"] == state), None)
                 for state in ("OPEN", "READY", "RECOVERED")}
        opened = first["OPEN"] or (rows[0]["recorded_at"] if rows else None)
        seconds = (round((stamp(first["RECOVERED"]) - stamp(opened)).total_seconds(), 1)
                   if opened and first["RECOVERED"] else None)
        result[name] = {"incident_id": incident_id, "opened_at": opened, "first_ready_at": first["READY"],
                        "recovered_at": first["RECOVERED"], "seconds_to_recovered": seconds}
    return result


def pair_result(pair, *, shop, stops, writes, ordering=None):
    """The pair's scored result against its pre-registered expectation.

    RECOVERED needs a RECOVERED shop and exactly the expected writes on each
    target; DEPENDENCY_DEADLOCK needs both incidents stopped in a mutual block
    with no write at all; a RECOVERED shop with other write counts is
    UNEXPECTED_WRITES; anything else is the shop's own status.

    For a dependency-ordered pair, ordered_recovery_shown is True only for a
    RECOVERED result whose ordering proof (ordering_proof) passed. An
    acceptable DEPENDENCY_DEADLOCK is a safe stop, never ordering evidence, so
    it is False then. It is None for an independent pair.
    """
    expected = pair["expected"]
    names = [item["target"] for item in pair["items"]]
    counts = {name: writes.get(name, 0) for name in names}
    if shop["status"] == "RECOVERED" and all(count == expected["writes_per_target"] for count in counts.values()):
        result = "RECOVERED"
    elif (pair["kind"] == "dependency_ordered" and all(stops.get(name) == "DEPENDENCY_DEADLOCK" for name in names)
          and not any(counts.values())):
        result = "DEPENDENCY_DEADLOCK"
    elif shop["status"] == "RECOVERED":
        result = "UNEXPECTED_WRITES"
    else:
        result = shop["status"]
    labels = pair.get("labels") or {}
    shown = (None if pair["kind"] != "dependency_ordered" else
             result == "RECOVERED" and isinstance(ordering, dict) and ordering.get("passed") is True)
    return {"pair": pair["id"], "kind": pair["kind"], "result": result, "expected": deepcopy(expected),
            "matched_expected": result == expected["result"], "acceptable": result in expected["acceptable"],
            "writes": counts, "stops": dict(stops), "labels": labels.get("status"),
            "labels_pending": labels.get("status") != "signed", "ordered_recovery_shown": shown}


def replan(incident, *, baselines, retriever, provider, live_template, resource_version, attempt):
    """A new proposal for an incident whose earlier one the Guard refused while a dependency was down.

    The incident and its evidence are unchanged; the proposal is bound to the
    target's current resource version, and its rationale says it is a re-plan,
    so its action ID differs from the refused one.
    """
    current = dict(deepcopy(incident), observed_resource_version=resource_version)

    def annotated(context):
        answer = provider(context)
        return {**answer, "rationale": answer["rationale"] + f" Re-planned (attempt {attempt}) after the "
                                                            "dependency that blocked the earlier proposal recovered."}

    return propose(current, baselines, retriever, annotated, live_template=live_template)


class PairRunner:
    """Advance a pair's incidents through the real workers until both settle or the budget ends.

    incidents maps each target name to its incident ID, in the order to act
    on them (the dependency's target first). advance(name, incident_id)
    moves one incident as far as it can now (runtime.recovery_runner.advance
    with readers that route by target) and returns its state.
    replan(name, incident_id, attempt) records a new proposal for a REJECTED
    incident and returns it. An approve callback that raises ApprovalRefused
    stops its incident, so a refused operator is never asked again. Nothing
    here reads a cluster.
    """

    def __init__(self, store, incidents, *, advance, replan, budget_seconds=DEFAULT_BUDGET_SECONDS,
                 max_replans=MAX_REPLANS, poll_seconds=POLL_SECONDS, clock=time.monotonic, sleep=time.sleep,
                 wall=utc):
        if not isinstance(incidents, dict) or len(incidents) != 2 or not all(incidents.values()):
            raise ValueError("A pair run needs exactly two recorded incidents")
        self.store, self.incidents = store, dict(incidents)
        self._advance, self._replan = advance, replan
        self.budget_seconds, self.max_replans, self.poll_seconds = budget_seconds, max_replans, poll_seconds
        self.clock, self.sleep, self.wall = clock, sleep, wall
        self.stops, self.replans, self.events = {}, {name: [] for name in incidents}, []

    def state(self, name):
        return self.store.incident(self.incidents[name])["state"]

    def refusal(self, name):
        """(reason codes, blockers) of the incident's active refused or escalated action."""
        action_id = self.store.incident(self.incidents[name])["active_action_id"]
        action = self.store.action(action_id) if action_id else None
        guard = (action or {}).get("guard_result") or {}
        finding = self.store.dependency_result(action_id) if action_id else None
        return list(guard.get("reason_codes", [])), list((finding or {}).get("blockers", []))

    def record(self, name, event, **details):
        self.events.append({"at": self.wall(), "target": name, "event": event, **details})

    def stop(self, name, reason, **details):
        self.stops[name] = reason
        self.record(name, "stopped", reason=reason, **details)

    def settled(self, name):
        return name in self.stops or self.state(name) in SETTLED

    def after_rejection(self, name):
        """Wait, re-plan or stop an incident whose active proposal the Guard REJECTED."""
        reasons, blockers = self.refusal(name)
        partners = set(self.incidents) - {name}
        if "DEPENDENCY_BLOCKED" not in reasons or not blockers or not set(blockers) <= partners:
            self.stop(name, "REJECTED", reason_codes=reasons, blockers=blockers)
            return
        states = {blocker: self.state(blocker) for blocker in blockers}
        if all(state == "RECOVERED" for state in states.values()):
            attempt = len(self.replans[name]) + 1
            if attempt > self.max_replans:
                self.stop(name, "REPLAN_BUDGET_EXHAUSTED", blockers=blockers)
                return
            try:
                proposal = self._replan(name, self.incidents[name], attempt)
            except CoordinatorError as error:
                # For example a second RESTART for the same incident: the Coordinator's restart budget refuses it.
                self.stop(name, "REPLAN_REFUSED", blockers=blockers, error=type(error).__name__)
                return
            self.replans[name].append(proposal["action_id"])
            self.record(name, "replanned", action_id=proposal["action_id"], attempt=attempt,
                        after_recovered=blockers)
            return
        for blocker in blockers:
            if blocker in self.stops and self.stops[blocker] != "DEPENDENCY_DEADLOCK":
                self.stop(name, "BLOCKER_NOT_RECOVERED", blockers=blockers, blocker_stop=self.stops[blocker])
                return
            if states[blocker] in SETTLED - {"RECOVERED"}:
                self.stop(name, "BLOCKER_NOT_RECOVERED", blockers=blockers, blocker_state=states[blocker])
                return
            if states[blocker] == "REJECTED":
                their_reasons, their_blockers = self.refusal(blocker)
                if "DEPENDENCY_BLOCKED" in their_reasons and name in their_blockers:
                    # Each repair waits for the other: stop both, write nothing, and leave it to a person.
                    self.stop(name, "DEPENDENCY_DEADLOCK", blockers=blockers)
                    self.stop(blocker, "DEPENDENCY_DEADLOCK", blockers=their_blockers)
                    return
        # The blocker is still being repaired: wait for it.

    def escalated(self, name):
        """True when the Guard escalated for something other than the operator's approval (stops the incident)."""
        if self.state(name) != "AWAITING_APPROVAL":
            return False
        reasons, _ = self.refusal(name)
        if reasons and reasons != ["OPERATOR_APPROVAL_REQUIRED"]:
            self.stop(name, "ESCALATED", reason_codes=reasons)
            return True
        return False

    def run(self):
        deadline = self.clock() + self.budget_seconds
        last = {}
        while True:
            for name, incident_id in self.incidents.items():
                if self.settled(name):
                    continue
                if self.state(name) == "REJECTED":
                    self.after_rejection(name)
                    if name in self.stops or self.state(name) == "REJECTED":
                        continue
                if self.escalated(name):
                    continue
                try:
                    state = self._advance(name, incident_id)
                except ApprovalRefused:
                    self.stop(name, "APPROVAL_REFUSED")
                    continue
                if last.get(name) != state:
                    self.record(name, "state", state=state)
                    last[name] = state
            if all(self.settled(name) for name in self.incidents):
                exhausted = False
                break
            if self.clock() >= deadline:
                exhausted = True
                break
            self.sleep(self.poll_seconds)
        states = {name: self.state(name) for name in self.incidents}
        return {"states": states, "stops": dict(self.stops), "replans": deepcopy(self.replans),
                "budget_exhausted": exhausted, "events": deepcopy(self.events)}


# The live harness. Never run: see docs/dual-fault.md and L-OVERLAPPING-FAULTS.

class PairBoutiqueExperiment(ChaosRecoveryMixin, ConnectedBoutiqueExperiment):
    """Two catalog faults on the connected lab at once, each repaired and verified on its own.

    One harness process drives both targets. acting_as(name) points the
    single-target helpers it reuses (deployment(), probes, verification
    samples, injection, attribution) at one target; every Guard, Executor and
    Verifier reader routes by the proposal's own target instead, because a
    pipeline pass can handle both incidents' requests. Each target has its
    own policy file (its ceilings, registry, service account and health
    rules) and baseline; the Executor allowlist, Role and approval authority
    name both. The harness collects one target's verification window at a
    time, so the other incident waits during it: timings measure this one
    process, not two independent workers.
    """

    verification_scope = "target"

    def __init__(self, directory, *, pair_id, storefront_port=8080, executor_kill=False,
                 budget_seconds=DEFAULT_BUDGET_SECONDS):
        super().__init__(directory, storefront_port=storefront_port)
        self.pair = scenarios.pair(pair_id)
        self.order = pair_order(self.pair)
        by_target = {item["target"]: item["id"] for item in self.pair["items"]}
        self.lanes = {name: {"scenario_id": by_target[name], "folder": self.directory / ("pair-" + name)}
                      for name in self.order}
        # The Executor is killed after the write of the pair's configuration (ROLLBACK) fault.
        rollback = [item["target"] for item in self.pair["items"] if item["template_changed"]]
        require(not executor_kill or len(rollback) == 1, "The Executor-kill variant needs one configuration fault")
        self.kill_target = rollback[0] if executor_kill else None
        self.killed = False
        self.budget_seconds = budget_seconds
        self.name, self.scenario_id = self.order[0], by_target[self.order[0]]
        self.summary.update(recovery_targets=list(self.order), recovery_target=None,
                            pair={"id": self.pair["id"], "kind": self.pair["kind"], "order": list(self.order),
                                  "scenarios": list(self.pair["scenarios"]), "labels": self.pair["labels"]["status"],
                                  "executor_kill": self.kill_target},
                            scope="Dedicated single-node connected lab; two simultaneous faults; one harness process")

    # Per-target context.

    @contextmanager
    def acting_as(self, name):
        """Point the single-target helpers at name; restores the previous target on exit."""
        lane = self.lanes[name]
        saved = (self.name, self.scenario_id, self.target, self.baseline, getattr(self, "policy", None),
                 getattr(self, "observer_collector", None))
        self.name, self.scenario_id = name, lane["scenario_id"]
        self.target, self.baseline = lane.get("target"), lane.get("baseline")
        self.policy = lane.get("policy")
        self.observer_collector = lane.get("collector")
        try:
            yield lane
        finally:
            (self.name, self.scenario_id, self.target, self.baseline, self.policy,
             self.observer_collector) = saved

    def lane_of(self, message):
        """The lane of a proposal, execution, incident or dependency message: by its own target."""
        return self.lanes[message["target"]["name"]]

    def policy_path(self, name):
        return self.directory / "policies" / (name + ".json")

    def readiness_path(self):
        """One dependency-readiness file per target, so the other target's read never overwrites it."""
        return self.directory / ("dependency-readiness-" + self.name + ".json")

    def executor_names(self):
        """Every target enabled in the connected scope plus both pair targets (redis-cart only in its pair)."""
        names = set(targets.enabled_targets(self.scope())) | set(self.order)
        return sorted(names)

    def install_probe_schema(self):
        """One ConfigMap holding every vendored proto the pair's probes need."""
        protos = sorted({targets.profile(name).probe["proto_file"] for name in self.order} - {None})
        if not protos:
            return
        configmap = {"apiVersion": "v1", "kind": "ConfigMap",
                     "metadata": {"name": PROBE_PROTO_CONFIGMAP, "namespace": self.namespace},
                     "data": {Path(proto).name: (ROOT / proto).read_text(encoding="utf-8") for proto in protos}}
        path = self.directory / "probe-proto.json"
        save(path, configmap)
        self.kubectl("apply", "-f", str(path))

    def approval_authority(self):
        """One approval authority whose identities may act on exactly the pair's two measured targets."""
        if self.authority is None:
            require(self.approval_mode in MODES, "This scenario requests no approval")
            first, second = (self.lanes[name].get("target") for name in self.order)
            require(first is not None and second is not None, "The approval authority needs both measured targets")
            authority = LocalApprovalAuthority(self.directory, self.directory / "coordinator.sqlite", first,
                                               operator=operator_identity(self.approval_mode, self.operator),
                                               additional_targets=[second])
            authority.start()
            self.authority = authority
            self.approver = approver_for(self.approval_mode, authority, relay=None)
            self.summary["operator_approval"] = self.approver.describe()
        return self.authority

    # Setup.

    def prepare_image(self):
        image = ConnectedBoutiqueExperiment.prepare_image(self)
        for name in self.order:
            with self.acting_as(name) as lane:
                self.observer_collector = KubernetesEvidenceCollector(
                    KubectlReader(self.context), namespace=self.namespace, name=name,
                    cluster_id=self.cluster_id, collect_logs=True)
                if self.scenario["injection"]["kind"] == "chaos":
                    self.wrap_observer()  # The chaos target detects with the metrics bridge and its thresholds.
                lane["collector"] = self.observer_collector
        return image

    def lane_policy(self, name):
        """The live policy for one pair target: its own ceilings and health rules; RESTART under the budget."""
        with self.acting_as(name):
            policy = self.executor_policy()
            policy.update(policy_id="executor-live-pair-" + name, policy_version=PAIR_POLICY_VERSION,
                          allowed_actions=["ROLLBACK", "RESTART"],
                          restart_constraints={"targets": [deepcopy(self.target)], **RESTART_BUDGET})
            health = self.health_rules()
            health.update(required_dependencies=self.required_dependencies(), max_sample_gap_seconds=90)
            policy["health"] = health
            return policy

    def prepare(self):
        for item in self.pair["items"]:
            scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context, namespace=self.namespace)
        chaos.require_connected(self.context, self.namespace)
        if any(item["injection"]["kind"] == "chaos" for item in self.pair["items"]):
            self.summary["chaos_preflight"] = preflight(self.chaos_cluster())
        image = self.prepare_image()
        for name in self.order:
            with self.acting_as(name) as lane:
                lane["folder"].mkdir(exist_ok=True)
                deployment = self.deployment()
                self.target = {"cluster_id": self.cluster_id, "namespace": self.namespace,
                               "kind": "Deployment", "name": name, "uid": deployment["metadata"]["uid"]}
                lane["target"] = self.target
                template = deployment["spec"]["template"]
                samples = self.health_window(template)
                lane["baseline"] = self.measured_baseline(template, samples)
                save(self.directory / "baselines" / (name + ".json"), lane["baseline"])
                save(lane["folder"] / "baseline-health.json", samples)
        with self.acting_as(self.order[0]):
            save(self.directory / "allowed-targets.json", self.execution_scope())
        for name in self.order:
            self.lanes[name]["policy"] = self.lane_policy(name)
            save(self.policy_path(name), self.lanes[name]["policy"])
        self.issue_restricted_credentials()
        self.summary.update(planning="Code rule provider with a trusted live-template read; no model call",
                            verification="Per target: own probe, readiness, template and callees; the storefront "
                                         "is judged separately as a ShopOutcome")
        self.check("healthy baselines captured for both pair targets", image=image, targets=list(self.order))

    # Observation, injection and detection.

    def observe(self, label):
        """Role 2 for the acting target, with its own Observer checkpoint and evidence files."""
        observation = self.observer_collector.collect()
        policy = {"detection": {"failure_duration_seconds": 5, "max_sample_gap_seconds": 45}}
        with ObserverStore(self.directory / ("observer-" + self.name + ".sqlite"), policy,
                           self.observer_collector.target) as store:
            incident = store.observe(observation)
            delivery = store.flush(LocalCoordinatorSink(self.directory / "coordinator.sqlite"))
            require(delivery["error"] is None, "Observer could not deliver its persisted incident")
        save(self.directory / (self.name + "-" + label + "-observation.json"), observation)
        if self.metrics_bridge is not None and self.scenario["injection"]["kind"] == "chaos":
            save(self.directory / "metrics-bridge.json", self.metrics_bridge.reports)
        if incident:
            save(self.directory / (self.name + "-" + label + "-incident.json"), incident)
        return incident

    def inject_pair(self):
        """Apply both faults, the configuration fault first; both within INJECTION_SPREAD_SECONDS."""
        applied = {}
        names = sorted(self.order, key=lambda name: self.lanes[name]["scenario_id"] in
                       {item["id"] for item in self.pair["items"] if item["injection"]["kind"] == "chaos"})
        for name in names:
            with self.acting_as(name):
                self.inject_fault()
                chaos_fault = self.scenario["injection"]["kind"] == "chaos"
                # A chaos object counts from its creation, not from Chaos Mesh's later confirmation.
                applied[name] = self.chaos_record["created_at"] if chaos_fault else live_utc()
                self.phase("fault_injected", name)
        stamps = sorted(datetime.fromisoformat(value.replace("Z", "+00:00")) for value in applied.values())
        spread = round((stamps[-1] - stamps[0]).total_seconds(), 1)
        self.summary["pair_injection"] = {
            "applied_at": applied, "spread_seconds": spread, "limit_seconds": INJECTION_SPREAD_SECONDS,
            # Approximate: neither stamp is when the fault took effect in the cluster.
            "basis": "configuration fault: after kubectl returned; chaos fault: just before the create call"}
        require(spread <= INJECTION_SPREAD_SECONDS,
                f"The two faults were applied {spread} s apart; the pair needs them within {INJECTION_SPREAD_SECONDS} s")
        self.check("both faults applied together", spread_seconds=spread)
        return applied

    def detect_pair(self):
        """Poll both Observers until each target has its own incident, within DETECTION_SECONDS."""
        deadline, index, incidents, errors = time.monotonic() + DETECTION_SECONDS, 0, {}, {}
        while len(incidents) < len(self.order) and time.monotonic() < deadline:
            for name in self.order:
                if name in incidents:
                    continue
                with self.acting_as(name) as lane:
                    try:
                        incident = self.observe("pair-" + str(index))
                        errors[name] = 0
                    except CollectionError:
                        # A rollout can change while objects are being read; retry bounded read-only collection.
                        errors[name] = errors.get(name, 0) + 1
                        if errors[name] >= 3:
                            raise
                        incident = None
                    if incident:
                        lane["incident"] = incident
                        incidents[name] = incident
                        self.phase("incident_observed", name)
            index += 1
            if len(incidents) < len(self.order):
                time.sleep(2)
        missing = sorted(set(self.order) - set(incidents))
        require(not missing, "Role 2 detected no incident for: " + ", ".join(missing))
        require(len({item["incident_id"] for item in incidents.values()}) == 2, "Both faults share one incident")
        impact = self.storefront.journey()
        self.record_shopping("pair-fault", impact)
        save(self.directory / "pair-storefront-impact.json", impact)
        self.check("an incident observed for each target", incidents={name: incidents[name]["incident_id"]
                                                                         for name in self.order})
        return incidents

    # Routed readers: every pipeline pass may hold both incidents' requests.

    def dependency_result(self, proposal):
        with self.acting_as(proposal["target"]["name"]):
            return super().dependency_result(proposal)

    def guard_state(self, proposal):
        with self.acting_as(proposal["target"]["name"]):
            return super().guard_state(proposal)

    def lane_baselines(self, incident):
        return [deepcopy(self.lane_of(incident)["baseline"])]

    def lane_template(self, incident):
        with self.acting_as(incident["target"]["name"]):
            return self.deployment()["spec"]["template"]

    def lane_samples(self, execution):
        """The acting-target verification reader, re-entered for each reading so no target leaks between them."""
        name = execution["target"]["name"]
        with self.acting_as(name):
            readings = self.verification_samples(execution)

        def stream():
            while True:
                with self.acting_as(name):
                    try:
                        sample = next(readings)
                    except StopIteration:
                        return
                yield sample
        return stream()

    def adapters(self, folder, *, coordinator=False, reconcile_only=False):
        """The restricted Executor for the acting target, with that target's own policy file."""
        adapters = build_adapters(
            context=self.restricted_context, cluster_id=self.cluster_id, kubeconfig=self.credentials,
            allowed_targets=self.directory / "allowed-targets.json", ledger=self.directory / "executor.sqlite",
            locks=self.directory / "locks", policy=self.policy_path(self.name), **self.baseline_inputs(),
            dependencies=folder / "dependencies.json", approval=folder / "approval.json", server_dry_run=True,
            approval_reader=self.approval_authority().executor_client() if coordinator else None,
            reconcile_only=reconcile_only)
        if not reconcile_only:
            adapters.authorizer.dependency_reader = self.dependency_result
        return adapters

    # One incident's steps (runtime.recovery_runner.advance).

    def plan_pending(self):
        proposals = process_planner_requests(self.store, self.bus, baseline_reader=self.lane_baselines,
                                             retriever=retrieve_runbooks, provider=mock_provider,
                                             template_reader=self.lane_template)
        for proposal in proposals:
            save(self.lane_of(proposal)["folder"] / ("proposal-" + proposal["action_id"] + ".json"), proposal)
            self.phase("proposal_saved", proposal["target"]["name"])

    def assess(self, proposal):
        finding = self.dependency_result(proposal)
        save(self.lane_of(proposal)["folder"] / ("dependencies-" + proposal["action_id"] + ".json"), finding)
        handle(finding, self.store, self.bus)

    def guard_pending(self):
        decisions = process_guard_requests(
            self.store, self.bus, state_reader=self.guard_state,
            baseline_reader=lambda proposal: deepcopy(self.lane_of(proposal)["baseline"]),
            policy_reader=lambda proposal: deepcopy(self.lane_of(proposal)["policy"]),
            dependency_reader=self.dependency_result)
        for decision in decisions:
            action = self.store.action(decision["action_id"])
            lane = self.lane_of(action["proposal"])
            save(lane["folder"] / ("guard-" + decision["action_id"] + "-" + str(len(lane.setdefault("guard", [])))
                                   + ".json"), decision)
            lane["guard"].append({"action_id": decision["action_id"], "verdict": decision["verdict"],
                                  "reason_codes": decision["reason_codes"], "checked_at": decision["checked_at"]})

    def approve_pair(self, proposal, decision):
        record = self.request_approval(proposal, decision)
        if record is None:
            raise ApprovalRefused(proposal["action_id"])
        return record

    def execute_lane(self, proposal):
        """Run the restricted Executor for the acting target's one pending request (or the kill variant)."""
        lane = self.lanes[self.name]
        folder, action_id = lane["folder"], proposal["action_id"]
        pending = {self.store.route(row[0])["action_id"]
                   for row in self.store.pending_routes("executor.request", "executor.reconcile")}
        require(pending <= {action_id}, "Another target's Executor request is pending; one target runs at a time")
        save(folder / "approval.json", self.store.approval(action_id))
        save(folder / "dependencies.json", self.dependency_result(proposal))
        if self.name == self.kill_target and not self.killed:
            return self.kill_and_reconcile(folder, proposal)
        adapters = self.adapters(folder, coordinator=True)
        try:
            results = process_executor_requests(self.store, self.bus, adapters=adapters)
            writes = adapters.cluster.stats["write_attempts"]
            lane["write_attempts"] = lane.get("write_attempts", 0) + writes
            save(folder / ("execution-" + action_id + ".json"), results)
            if any(item["action_id"] == action_id and item["status"] == "APPLIED" for item in results):
                # #77: a changed payload under the reused action ID is refused before any write.
                changed = dict(proposal, rationale="changed intent under reused action ID")
                try:
                    execute(changed, adapters)
                except ActionConflict:
                    pass
                else:
                    raise RuntimeError("Changed intent under an existing action ID was accepted")
                require(adapters.cluster.stats["write_attempts"] == writes, "A changed action ID repeated a write")
                lane["changed_payload_refused"] = True
                self.phase("execution_recorded", self.name)
        finally:
            adapters.ledger.close()

    def kill_and_reconcile(self, folder, proposal):
        """Kill the Executor worker process right after its write, then reconcile without writing again."""
        self.killed = True
        save(folder / "proposal.json", proposal)
        configuration = {"directory": str(self.directory), "folder": str(folder), "credentials": str(self.credentials),
                         "cluster_id": self.cluster_id, "context": self.restricted_context,
                         "baselines": str(self.directory / "baselines"), "policy": str(self.policy_path(self.name))}
        save(folder / "worker.json", configuration)
        # The Executor token reaches the worker only through its environment, never argv or worker.json.
        worker = subprocess.run([sys.executable, str(self.entrypoint), "--crash-worker", str(folder / "worker.json")],
                                cwd=ROOT, capture_output=True, text=True, timeout=60,
                                env={**os.environ, **self.approval_authority().executor_environment()})
        require(worker.returncode == 73, "The Executor worker did not stop at the intended post-write boundary")
        with SQLiteExecutionLedger(self.directory / "executor.sqlite") as ledger:
            require(ledger.get(proposal["action_id"])["phase"] == "WRITE_STARTED",
                    "The killed worker completed its ledger entry unexpectedly")
        adapters = self.adapters(folder, coordinator=True, reconcile_only=True)
        try:
            results = process_executor_requests(self.store, self.bus, adapters=adapters)
            require(len(results) == 1 and results[0]["status"] == "APPLIED",
                    "Reconciliation did not find the killed worker's committed write")
            require(adapters.cluster.stats["write_attempts"] == 0, "Reconciliation issued another write")
            receipt = json.loads((folder / "write-receipt.json").read_text(encoding="utf-8"))
            require(receipt["stats"]["write_attempts"] == 1, "The killed worker issued more than one write")
            save(folder / "reconciliation.json", results[0])
        finally:
            adapters.ledger.close()
        lane = self.lanes[self.name]
        lane["write_attempts"] = lane.get("write_attempts", 0) + 1
        self.summary["executor_kill"] = {"target": self.name, "action_id": proposal["action_id"], "worker_exit": 73,
                                         "worker_writes": 1, "reconcile_writes": 0, "reconciled": "APPLIED"}
        self.check("Executor worker killed after its write; reconciliation found it without writing again",
                   target=self.name)

    def verify_lane(self, proposal, execution):
        """Role 6 for the acting target, in the target verification scope; records the outcome, never asserts it."""
        lane = self.lanes[self.name]
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=120s", timeout=160,
                     allow_failure=True)
        ledger = VerificationStore(self.directory / "verifier.sqlite3")
        try:
            outcomes = process_verifier_requests(self.store, self.bus, ledger=ledger, sample_reader=self.lane_samples,
                                                 policy_reader=lambda current: deepcopy(self.lane_of(current)["policy"]))
            job = ledger.get(execution["action_id"])
            if job:
                save(lane["folder"] / ("post-health-" + execution["action_id"] + ".json"), job["samples"])
        finally:
            ledger.close()
        for outcome in outcomes:
            folder = self.lane_of(outcome)["folder"]
            save(folder / ("outcome-" + outcome["action_id"] + ".json"), outcome)
            save(folder / "outcome.json", outcome)
            self.phase("outcome_recorded", outcome["target"]["name"])
        mine = [outcome for outcome in outcomes if outcome["action_id"] == proposal["action_id"]]
        if (mine and mine[0]["status"] == "RECOVERED" and proposal["action"] == "RESTART"
                and self.scenario["injection"]["kind"] == "chaos"):
            self.attribute(lane["folder"])  # Fails the run unless the RESTART, not the fault's expiry, recovered it.

    def advance_lane(self, name, incident_id):
        with self.acting_as(name):
            return advance_incident(self.store, incident_id, plan=self.plan_pending, assess=self.assess,
                                    guard=self.guard_pending, approve=self.approve_pair, execute=self.execute_lane,
                                    verify=self.verify_lane)

    def replan_lane(self, name, incident_id, attempt):
        with self.acting_as(name) as lane:
            deployment = self.deployment()
            proposal = replan(lane["incident"], baselines=[deepcopy(self.baseline)], retriever=retrieve_runbooks,
                              provider=mock_provider, live_template=deployment["spec"]["template"],
                              resource_version=deployment["metadata"]["resourceVersion"], attempt=attempt)
            save(lane["folder"] / ("replan-" + str(attempt) + ".json"), proposal)
            handle(proposal, self.store, self.bus)
            return proposal

    # The verdict.

    def shop_journeys(self):
        journeys = []
        for index in range(SHOP_PASSES):
            journey = self.storefront.journey()
            self.record_shopping("pair-shop", journey)
            journeys.append(journey)
            if index < SHOP_PASSES - 1:
                time.sleep(SHOP_INTERVAL_SECONDS)
        return journeys

    def scores(self, incidents):
        """Each scenario's first delivered decision against its own pre-registered labels."""
        graded = {}
        for name in self.order:
            with self.acting_as(name):
                actions = self.store.actions_for_incident(incidents[name])
                if not actions:
                    continue
                proposal = actions[0]["proposal"]
                guard = actions[0]["guard_result"] or {}
                outcome = scenarios.decision(proposal)
                graded[name] = scenarios.result(
                    self.scenario, outcome, source=scenarios.decision_source(proposal, decided_by="rule"),
                    incident_id=incidents[name], action_id=proposal["action_id"],
                    coordinator_state=self.store.incident(incidents[name])["state"],
                    guard_verdict=guard.get("verdict"), guard_reason_codes=list(guard.get("reason_codes", [])))
                save(self.lanes[name]["folder"] / "scenario-result.json", graded[name])
        return graded

    def judge(self, incidents, run):
        """Score the pair, save its evidence, and fail unless it ended in an expected or acceptable result."""
        ids = {name: incidents[name]["incident_id"] for name in self.order}
        journeys = [] if run["budget_exhausted"] else self.shop_journeys()
        shop = shop_outcome(run["states"], journeys, stopped=run["stops"])
        with SQLiteExecutionLedger(self.directory / "executor.sqlite") as ledger:
            writes = writes_by_target(ledger.db)
        transitions = self.store.transitions()
        ordering = None
        if self.pair["kind"] == "dependency_ordered":
            first, then = self.order
            ordering = ordering_proof(transitions, first_incident=ids[first], then_incident=ids[then])
        graded = pair_result(self.pair, shop=shop, stops=run["stops"], writes=writes, ordering=ordering)
        evidence = {"incidents": ids, "run": run, "shop": shop, "result": graded,
                    "cross_incident_transitions": cross_incident_transitions(self.store),
                    "timings": incident_timings(transitions, ids), "scenario_scores": self.scores(ids),
                    "changed_payload_refused": {name: self.lanes[name].get("changed_payload_refused", False)
                                                for name in self.order}}
        if ordering is not None:
            evidence["ordering"] = ordering
        save(self.directory / "pair-result.json", evidence)
        self.summary["pair_result"] = {key: evidence[key] for key in ("shop", "result", "timings")}
        self.summary["pair_result"]["ordering"] = evidence.get("ordering")
        require(not evidence["cross_incident_transitions"], "An incident changed state because of the other incident")
        require(graded["matched_expected"] or graded["acceptable"],
                f"Pair {self.pair['id']} ended {graded['result']}; expected {self.pair['expected']['result']}")
        if graded["result"] == "RECOVERED" and "ordering" in evidence:
            require(evidence["ordering"]["passed"], "The second target was allowed before the first RECOVERED")
        self.check(f"Pair {self.pair['id']} ended {graded['result']}", writes=graded["writes"],
                   shop=shop["status"], labels=graded["labels"])
        return evidence

    def emergency_restore(self):
        """Remove the chaos object and put each changed template back, as the administrator; never a repair."""
        try:
            self.remove_chaos()
        except Exception as error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "boutique.pair_chaos_cleanup.failed", error, level=log.ERROR)
            self.summary["chaos_cleanup_confirmed"] = False
        restored = {}
        for name in self.order:
            with self.acting_as(name):
                if self.baseline is None:
                    continue
                try:
                    changed = fingerprint(self.deployment()["spec"]["template"]) != self.baseline["template_sha256"]
                except Exception as error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
                    log.failure(LOG, "boutique.pair_restore_read.failed", error, level=log.ERROR)
                    changed = True
                restored[name] = Experiment.emergency_restore(self) if changed else "unchanged"
        self.summary["operator_emergency_restore"] = restored
        return restored

    def operator_cleanup(self):
        """After a passing run, put back every target configuration still left faulted, as the administrator.

        An accepted result that is not a recovery (DEPENDENCY_DEADLOCK, the
        predicted result for redis-pod+cart-config) leaves a configuration
        fault in place, for example cartservice's bad REDIS_ADDR. This runs
        after the pair was judged and saved, with the same conditional patch
        (UID and resourceVersion tested) as the drift cell's cleanup, and waits
        for the rollout. It is lab cleanup, never a Sentinel repair: the write
        is not in the Executor ledger and is recorded only in
        summary["operator_cleanup"]. A template that differs from its baseline
        only by Sentinel's restart annotation (a verified RESTART) is left alone.
        """
        cleanup = {}
        for name in self.order:
            with self.acting_as(name):
                template = self.deployment()["spec"]["template"]
                if config_fingerprint(template) == config_fingerprint(self.baseline["pod_template"]):
                    cleanup[name] = {"restored": False, "reason": "configuration at baseline"}
                    continue
                attempts = self.administrator_restore("operator-cleanup-" + name + "-patch.json", drift_cell=False)
                self.kubectl("rollout", "status", "deployment/" + name, "--timeout=180s", timeout=200)
                restored = self.deployment()["spec"]["template"]
                require(config_fingerprint(restored) == config_fingerprint(self.baseline["pod_template"]),
                        f"The administrator cleanup did not bring {name} back to its baseline configuration")
                cleanup[name] = {"restored": True, "patch_attempts": attempts, "rollout_complete": True,
                                 "by": "lab administrator; not a Sentinel repair"}
        self.summary["operator_cleanup"] = cleanup
        restored = sorted(name for name, item in cleanup.items() if item["restored"])
        if restored:
            self.check("Administrator put the remaining faulted configuration back after scoring (not a repair)",
                       targets=restored)
        return cleanup

    def run(self):
        try:
            self.prepare()
            self.permissions()
            self.inject_pair()
            incidents = self.detect_pair()
            runner = PairRunner(self.store, {name: incidents[name]["incident_id"] for name in self.order},
                                advance=self.advance_lane, replan=self.replan_lane,
                                budget_seconds=self.budget_seconds)
            run = runner.run()
            self.judge(incidents, run)
            self.remove_chaos()
            self.check("Chaos Mesh object deleted and its cleanup confirmed")
            for name in self.order:
                with self.acting_as(name):
                    # The state Sentinel left, before any administrator cleanup.
                    save(self.lanes[name]["folder"] / "final-deployment.json", self.deployment())
            self.operator_cleanup()
            self.summary["status"] = "PASS"
        except BaseException as exc:
            self.summary.update(status="FAILED", error_type=type(exc).__name__, error=str(exc)[:300])
            self.emergency_restore()
            raise
        finally:
            self.credentials.unlink(missing_ok=True)
            self.close_approval_authority()
            self.store.close()
            self.summary.update(finished_at=live_utc(),
                                private_executor_credentials_removed=not self.credentials.exists())
            save(self.directory / "summary.json", self.summary)


def run_pair(args, directory, *, tools):
    """The --pair entry point of scripts/test_boutique_live.py; returns the process exit code.

    The whole run holds the shared heavy-lab lock (experiments/lab_lock.py), so no P4.01 or P4.03 run
    overlaps it; a held or stale lock refuses it (exit 2) before anything is created.
    """
    lock = lab_lock.HeavyLabLock(holder="P4.05 pair " + args.pair,
                                 break_stale=getattr(args, "break_stale_lock", False))
    return lab_lock.run_holding(lock, lambda: _run_pair(args, directory, tools=tools))


def _run_pair(args, directory, *, tools):
    experiment = PairBoutiqueExperiment(directory, pair_id=args.pair, storefront_port=args.storefront_port,
                                        executor_kill=args.executor_kill)
    experiment.configure_approval(args.approval, args.operator)
    experiment.summary["provenance"]["tools"] = tools
    print("Boutique pair evidence: " + str(experiment.directory), flush=True)

    def stop_requested(_signum, _frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, stop_requested)
    try:
        experiment.run()
    except KeyboardInterrupt:
        experiment.summary.update(status="INTERRUPTED")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level boundary: record FAILED and exit 1
        log.failure(LOG, "experiment.failed", exc, level=log.ERROR)
        experiment.summary.update(status="FAILED", integration_error=type(exc).__name__)
        print("FAIL: Boutique pair experiment: " + str(exc), file=sys.stderr)
        return 1
    finally:
        try:
            experiment.close()
        finally:
            save(experiment.directory / "summary.json", experiment.summary)
            signal.signal(signal.SIGTERM, previous)
    result = experiment.summary["pair_result"]["result"]
    print(f"PASS: pair {result['pair']} ended {result['result']}; writes {result['writes']}; "
          f"labels {result['labels']}", flush=True)
    if result["labels_pending"]:
        print("WARNING: the pair labels are not signed off; do not report this trial as scored", flush=True)
    if result["ordered_recovery_shown"] is False:
        print(f"NOTE: {result['result']} is not an ordered recovery; this run is not evidence that the second "
              "repair was allowed only after the first RECOVERED", flush=True)
    return 0
