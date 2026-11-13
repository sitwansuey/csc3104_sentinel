"""Chaos Mesh faults wired into guarded recovery on the connected Boutique lab (P4.03).

The catalog scenarios N2 (pod failure), N3 (CPU stress) and T1 (network delay,
a true-negative control) run through the same Observer, Coordinator, Planner,
Guard, Executor and Verifier as the template faults. Only
kind-sentinel-boutique-connected is used; any name containing sentinel-lab is
always refused.

- Setup is install only. chaos.ensure_chaos_mesh runs the pinned Helm chart and
  the namespace annotation; ensure_metrics_server runs one pinned Helm chart
  whose image must be pinned by digest. Both first read the node list and
  refuse a context that does not reach the connected lab's single node.
  preflight() only reads.
- Detection. In every chaos run the Observer collector is wrapped with the
  metrics-server bridge (runtime/metrics_bridge.py) and the scenario's metric
  thresholds, so the control uses the same detection rules as the stress case.
- Planning. Without --agent the code rule provider (mock_provider in
  services/planner/core.py) plans with a trusted read of the live template:
  Pods failing while the configuration equals the baseline give RESTART, and a
  metric-only incident is escalated by the Planner's metric-only rule.
- Attribution (attribution()). A RESTART counts as Sentinel's repair only when
  the Verifier said RECOVERED, no current Pod is one the chaos selected (by the
  UID recorded at injection, or by name in the chaos object's containerRecords),
  the outcome window ended at least ATTRIBUTION_MARGIN_SECONDS before the fault
  expires, and the same chaos object was still present and running after the
  outcome. Otherwise the fault's own expiry could have healed the target.
- Forced refusals (forced_refusal()). A harness-forced ROLLBACK during the pod
  failure, a forced RESTART during CPU stress and a forced RESTART during a
  template drift go to the restricted Executor with no approval: nobody approves
  a wrong action, and the harness never writes one for itself. Its fresh Guard
  check must REJECT with the expected code, with zero write attempts. When the
  live state makes the forced action pass the Guard's checks (for example a
  stressed Pod that also fails readiness), the missing approval makes the Guard
  ESCALATE, the Executor writes nothing, and the run fails as not refused.
- True negative (true_negative_proof()). No incident passes only with the fault
  confirmed injected, Observer samples spanning the detection window, at least
  one complete metric-threshold reading, no Coordinator action, no Executor
  ledger action, and the template still at the baseline.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import re
import shutil
import time
from uuid import uuid4

from experiments import lab_lock
from experiments.boutique.harness import chaos, scenarios
from experiments.boutique.harness.chaos_demo import OWNER, owned_remove
from experiments.boutique.harness.durable_case import run_durable_case
from experiments.boutique.harness.live import BoutiqueExperiment
from experiments.boutique.harness.scoring import ScenarioScoringMixin
from experiments.recovery.live import require, save, utc
from runtime.metrics_bridge import MetricsBridgeCollector
from services.executor.core import execute
from services.executor.store import SQLiteExecutionLedger, fingerprint
from services.observer.kubernetes import KubectlReader
from services.observer.telemetry import CorrelatedEvidenceCollector, TelemetryStore
from services.planner.core import mock_provider, retrieve_runbooks
from services.planner.store import PlannerStore
from shared import log
from shared.actions import restart_parameters

LOG = log.get_logger("experiment")

ATTRIBUTION_MARGIN_SECONDS = 60
INJECTION_CONFIRM_SECONDS = 60
# Rule-mode planning keeps its journal and evidence here, like agent mode's approved-rollback folder.
PLANNING_FOLDER = "chaos-recovery"
# At most one RESTART per target per hour in a chaos run's Executor ledger.
RESTART_BUDGET = {"max_restarts": 1, "window_seconds": 3600}
METRICS_SERVER_CHART = "3.12.2"
METRICS_SERVER_REPOSITORY = "https://kubernetes-sigs.github.io/metrics-server/"
METRICS_SERVER_TAG = "v0.7.2"
_DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
_POD_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")
# Expected Guard reason codes for each forced wrong action.
FORCED = {
    ("pod", "ROLLBACK"): "ROLLBACK_ALREADY_AT_BASELINE",
    ("cpu", "RESTART"): "RESTART_NOT_JUSTIFIED",
    ("drift", "RESTART"): "RESTART_TEMPLATE_DRIFT",
}
# A forced proposal whose resourceVersion went stale before the Executor read it (a rollout in
# progress) gets CONFLICT with no Guard decision; it is retried with a fresh proposal this many times.
FORCED_STALE_RETRIES = 3
FORCED_STALE_WAIT_SECONDS = 2
# Before the drift cell's forced proposal: wait (bounded) until the drift's Deployment stops changing.
DRIFT_SETTLE_SECONDS = 30
DRIFT_SETTLE_POLL_SECONDS = 2


def owner_label(name):
    """A Kubernetes label value naming this run, so cleanup never deletes another run's chaos object."""
    value = re.sub(r"[^A-Za-z0-9_.-]", "-", str(name))[-63:].strip("-_.")
    return value or "run"


def _stamp(value):
    moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if moment.utcoffset() is None:
        raise ValueError("Expected a timezone-aware time")
    return moment.astimezone(UTC)


# Setup: install only, never a cluster.

def metrics_server_argv(cluster, image_digest, helm="helm"):
    """One pinned Helm chart for metrics-server, its image pinned by digest; an exact argument list."""
    chaos.require_connected(cluster.context, cluster.namespace)
    if not isinstance(image_digest, str) or not _DIGEST.fullmatch(image_digest):
        raise ValueError("The metrics-server image must be pinned by digest: sha256:<64 hex>")
    return [helm, "upgrade", "--install", "metrics-server", "metrics-server", "--repo", METRICS_SERVER_REPOSITORY,
            "--version", METRICS_SERVER_CHART, "--kube-context", cluster.context, "--namespace", "kube-system",
            "--values", str(chaos.ROOT / "deploy/metrics-server/values.yaml"),
            "--set-string", "image.tag=" + METRICS_SERVER_TAG + "@" + image_digest, "--wait", "--timeout", "5m"]


def ensure_metrics_server(cluster, image_digest, helm="helm"):
    """Install only: the pinned metrics-server chart into kube-system of the connected lab."""
    argv = metrics_server_argv(cluster, image_digest, helm)
    if not shutil.which(helm):
        raise RuntimeError(f"Missing {helm}; install it or supply its executable path")
    chaos.require_connected_nodes(cluster)
    cluster.command(argv, timeout=360)


def preflight(cluster):
    """Read-only checks before a chaos trial; returns the facts, raises on the first failed check.

    The Chaos Mesh CRDs exist, the chaos-mesh controller and daemon Pods are
    ready, the connected namespace opts in to injection, no chaos object is
    left in it from an earlier run, and the Metrics API answers for it.
    """
    chaos.require_connected(cluster.context, cluster.namespace)
    facts = {"context": cluster.context, "namespace": cluster.namespace}
    for kind in ("podchaos", "stresschaos", "networkchaos"):
        cluster.call("get", "crd", kind + ".chaos-mesh.org", "-o", "name")
    pods = cluster.get("pods", namespace="chaos-mesh")["items"]
    ready = {pod["metadata"]["name"]: all(status.get("ready") is True for status in
                                          pod.get("status", {}).get("containerStatuses", []) or [{}])
             for pod in pods}
    require(any(name.startswith("chaos-controller-manager") for name in ready)
            and any(name.startswith("chaos-daemon") for name in ready) and all(ready.values()),
            "Chaos Mesh controller and daemon Pods must all be ready in chaos-mesh")
    facts["chaos_mesh_pods"] = sorted(ready)
    namespace = cluster.get("namespace", cluster.namespace)
    require(namespace["metadata"].get("annotations", {}).get("chaos-mesh.org/inject") == "enabled",
            "The connected namespace does not opt in to Chaos Mesh injection")
    leftovers = []
    for kind in ("podchaos", "stresschaos", "networkchaos"):
        leftovers += [kind + "/" + item["metadata"]["name"] for item in cluster.get(kind)["items"]]
    require(not leftovers, "Chaos objects are left in the connected namespace: " + ", ".join(leftovers[:5]))
    metrics = cluster.raw("/apis/metrics.k8s.io/v1beta1/namespaces/" + cluster.namespace + "/pods")
    facts["pod_metrics_items"] = len(metrics.get("items", []))
    # With no Pod metrics the CPU thresholds can never be complete, so N3 and T1 could not alert or prove a negative.
    require(facts["pod_metrics_items"] > 0, "The Metrics API returned no Pod metrics for the connected namespace")
    return facts


# The chaos record and the attribution proof (pure).

def record_ids(document):
    """The ids of the chaos object's containerRecords, in order."""
    records = document.get("status", {}).get("experiment", {}).get("containerRecords", []) or []
    return [str(record.get("id")) for record in records if isinstance(record, dict)]


def record_pod_names(ids, namespace):
    """Pod names from containerRecords ids "<namespace>/<pod>" or "<namespace>/<pod>/<container>".

    Any other shape, or another namespace, raises ValueError: an unparsed
    record must stop the trial, never be ignored.
    """
    names = []
    for value in ids:
        parts = value.split("/")
        if len(parts) not in (2, 3) or parts[0] != namespace or not _POD_NAME.fullmatch(parts[1]):
            raise ValueError("Unexpected Chaos Mesh record id: " + value[:120])
        names.append(parts[1])
    return sorted(set(names))


def chaos_record(document, *, created_at, duration_seconds, pods):
    """What attribution needs about one injected chaos object; pods maps each selected Pod name to its UID."""
    metadata = document["metadata"]
    ids = record_ids(document)
    names = record_pod_names(ids, metadata["namespace"])
    require(names and set(names) == set(pods), "Every selected Pod needs its UID recorded at injection")
    expires = _stamp(created_at) + timedelta(seconds=duration_seconds)
    return {"kind": document["kind"], "name": metadata["name"], "namespace": metadata["namespace"],
            "uid": metadata["uid"], "spec": deepcopy(document.get("spec", {})),
            "created_at": created_at, "duration_seconds": duration_seconds, "expires_at": utc(expires),
            "record_ids": ids, "selected_pods": [{"name": name, "uid": pods[name]} for name in names]}


def attribution(record, outcome, *, current_pods, chaos_now, margin_seconds=ATTRIBUTION_MARGIN_SECONDS):
    """Whether a RECOVERED outcome can be attributed to Sentinel's repair rather than the fault's expiry.

    record is chaos_record() from injection; outcome is the Verifier outcome;
    current_pods are the target's Pods ({name, uid}) read after the outcome;
    chaos_now is the chaos object read after the outcome, or None when it is
    gone. Every check must pass; a check that cannot be made fails.
    """
    checks = []

    def check(name, passed, detail):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    check("outcome_recovered", outcome.get("status") == "RECOVERED", outcome.get("status"))
    selected_uids = {pod["uid"] for pod in record["selected_pods"]}
    selected_names = {pod["name"] for pod in record["selected_pods"]}
    try:
        now_names = set(record_pod_names(record_ids(chaos_now), record["namespace"])) if chaos_now else set()
    except ValueError:
        now_names = None
    reused = sorted(pod["uid"] for pod in current_pods if pod["uid"] in selected_uids)
    named = sorted(pod["name"] for pod in current_pods
                   if pod["name"] in selected_names or (now_names is not None and pod["name"] in now_names))
    check("new_pods_not_selected", current_pods and not reused and not named and now_names is not None,
          {"current_pods": [pod["uid"] for pod in current_pods], "selected_uids_still_running": reused,
           "pods_named_in_records": named, "records_parsed": now_names is not None})
    try:
        deadline = _stamp(record["expires_at"]) - timedelta(seconds=margin_seconds)
        ended = _stamp(outcome["window_end"])
        early = ended < deadline
        detail = {"window_end": outcome["window_end"], "latest_allowed": utc(deadline),
                  "seconds_before_expiry": round((_stamp(record["expires_at"]) - ended).total_seconds(), 1)}
    except (KeyError, TypeError, ValueError):
        early, detail = False, "outcome or expiry time unavailable"
    check("recovered_before_expiry", early, detail)
    metadata = (chaos_now or {}).get("metadata", {})
    phase = (chaos_now or {}).get("status", {}).get("experiment", {}).get("desiredPhase")
    running = (chaos_now is not None and metadata.get("uid") == record["uid"]
               and not metadata.get("deletionTimestamp") and phase == "Run")
    check("chaos_still_running", running, {"present": chaos_now is not None, "same_object": metadata.get("uid") == record["uid"],
                                           "desired_phase": phase, "deleting": bool(metadata.get("deletionTimestamp"))})
    return {"attributed": all(item["passed"] for item in checks), "checks": checks,
            "chaos": {key: record[key] for key in ("kind", "name", "uid", "created_at", "expires_at")}}


def true_negative_proof(*, injected, observations, window_seconds, thresholds_complete, coordinator_incidents,
                        coordinator_actions, executor_actions, template_sha256, baseline_sha256):
    """No incident is a result only with positive evidence that nothing was missed and nothing written.

    observations are the Observer's capture times during the detection window;
    thresholds_complete counts the readings whose metric thresholds had full
    Pod coverage (no alert is possible without one).
    """
    stamps = sorted(_stamp(value) for value in observations)
    span = (stamps[-1] - stamps[0]).total_seconds() if len(stamps) > 1 else 0
    checks = {"fault_injected": injected is True,
              "observer_window_covered": span >= window_seconds * 0.8,
              "metric_thresholds_evaluated": thresholds_complete > 0,
              "no_incident": coordinator_incidents == 0,
              "no_coordinator_action": coordinator_actions == 0,
              "no_executor_action": executor_actions == 0,
              "template_at_baseline": template_sha256 == baseline_sha256}
    return {"passed": all(checks.values()), "checks": checks, "observations": len(stamps),
            "observed_seconds": round(span, 1), "thresholds_complete_readings": thresholds_complete,
            "executor_writes": 0 if executor_actions == 0 else None}


# The harness.

class ChaosRecoveryMixin(ScenarioScoringMixin):
    """Mix in before ConnectedBoutiqueExperiment (and before AgentPlanningMixin for --agent)."""

    chaos_record = None
    chaos_document = None
    metrics_bridge = None
    agent_mode = False
    expect = "recovered"

    @property
    def chaos_fault(self):
        return self.scenario["injection"]["chaos"]

    def chaos_cluster(self):
        return chaos.Cluster(self.context, self.namespace)

    def prepare(self):
        item = self.scenario
        require(item["injection"]["kind"] == "chaos", "The chaos harness runs only Chaos Mesh scenarios")
        scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context, namespace=self.namespace)
        chaos.require_connected(self.context, self.namespace)
        self.summary["chaos_preflight"] = preflight(self.chaos_cluster())
        super().prepare()
        # RESTART is allowed under a one-per-hour budget, so the Guard decides each RESTART on its merits.
        self.policy["allowed_actions"] = ["ROLLBACK", "RESTART"]
        self.policy["restart_constraints"] = {"targets": [deepcopy(self.target)], **RESTART_BUDGET}
        save(self.directory / "policy.json", self.policy)

    def prepare_image(self):
        image = super().prepare_image()
        self.wrap_observer()
        return image

    def wrap_observer(self):
        """Every chaos run detects with the metrics-server bridge and the scenario's thresholds."""
        store = TelemetryStore(self.directory / "observer-metrics.sqlite3")
        self.metrics_bridge = MetricsBridgeCollector(self.observer_collector, KubectlReader(self.context), store,
                                                     service=self.name)
        self.observer_collector = CorrelatedEvidenceCollector(
            self.metrics_bridge, store, [self.name],
            {"max_age_seconds": 60, "metric_thresholds": deepcopy(self.scenario["observer_thresholds"])})

    def observe(self, label):
        incident = super().observe(label)
        if self.metrics_bridge is not None:
            save(self.directory / "metrics-bridge.json", self.metrics_bridge.reports)
        return incident

    # Injection and cleanup.

    def inject_fault(self):
        item = self.scenario
        if item["injection"]["kind"] != "chaos":
            return super().inject_fault()
        scenarios.require_scope(item, cluster_id=self.cluster_id, context=self.context, namespace=self.namespace)
        require(item["target"] == self.name, "The scenario targets another workload")
        injection = item["injection"]
        cluster = self.chaos_cluster()
        name = "sentinel-" + injection["chaos"] + "-" + uuid4().hex[:10]
        document = chaos.scenario(injection["chaos"], self.namespace, name, injection["duration_seconds"], self.name)
        document["metadata"]["labels"][OWNER] = owner_label(self.directory.name)
        save(self.directory / "chaos-request.json", document)
        cluster.create(document, dry_run=True)
        created_at = utc()  # Before creation: the expiry measured from here is never later than the real one.
        # Known before the create call: if the API server made the object but the call still failed, cleanup can
        # remove it (owned_remove ignores an object that does not exist and refuses one another run owns).
        self.chaos_document = document
        cluster.create(document)
        deadline = time.monotonic() + INJECTION_CONFIRM_SECONDS
        while True:
            current = cluster.get(document["kind"], name)
            if chaos.injected(current):
                break
            require(time.monotonic() < deadline, "Chaos Mesh did not confirm injection into the selected Pod")
            time.sleep(1)
        pods = {}
        for pod_name in record_pod_names(record_ids(current), self.namespace):
            pod = cluster.get("pod", pod_name)
            require(pod["metadata"].get("labels", {}).get("app") == self.name,
                    "Chaos Mesh selected a Pod that is not the target's")
            pods[pod_name] = pod["metadata"]["uid"]
        self.chaos_record = chaos_record(current, created_at=created_at,
                                         duration_seconds=injection["duration_seconds"], pods=pods)
        save(self.directory / "chaos-experiment.json", self.chaos_record)
        self.summary.setdefault("fault_injected_at", created_at)
        self.summary["chaos"] = {key: self.chaos_record[key] for key in ("kind", "name", "expires_at")}
        self.event(1, "fault", f"Operator injected Chaos Mesh {injection['chaos']} into {self.name} for "
                   f"{injection['duration_seconds']} s", target=self.target)

    def remove_chaos(self):
        """Delete this run's chaos object and wait for its finalizers; never strip them."""
        if self.chaos_document is None:
            return True
        cluster = self.chaos_cluster()
        owned_remove(cluster, self.chaos_document["kind"], self.chaos_document["metadata"]["name"],
                     self.chaos_document["metadata"]["labels"][OWNER])
        self.summary["chaos_cleanup_confirmed"] = True
        return True

    def emergency_restore(self):
        """Remove the fault as the administrator; never counted as Sentinel recovery."""
        try:
            self.remove_chaos()
        except Exception as error:  # noqa: BLE001 - recorded; a failed run re-raises its original error
            log.failure(LOG, "boutique.chaos_cleanup.failed", error, level=log.ERROR)
            self.summary["chaos_cleanup_confirmed"] = False
        return super().emergency_restore()

    def fault(self, label):
        if self.chaos_fault == "pod":
            return super().fault(label)  # The catalog predicts the storefront impact of an unready shipping Pod.
        # CPU stress and network delay have no pre-registered storefront impact: record it, never require it.
        incident = BoutiqueExperiment.fault(self, label)
        impact = self.storefront.journey()
        self.record_shopping(label + "-fault", impact, incident_id=(incident or {}).get("incident_id"))
        save(self.directory / (label + "-storefront-impact.json"), impact)
        return incident

    # Scoring, planning and the forced refusals.

    def no_incident(self, label):
        """Score NO_INCIDENT with a true-negative proof; continue only when the label allows no incident.

        The run continues when NO_INCIDENT scores correct (T1) or acceptable (N3,
        whose CPU cell then still runs) and the true-negative proof passes. The
        score saved in scenario-result.json is the one the label gives; an
        acceptable result is never recorded as correct.
        """
        item = self.scenario
        self.summary["expected_outcome"] = self.expect
        proof = self.true_negative()
        facts = self.scenario_facts(None, None, None, self.directory)
        facts.update(executor_writes=proof["executor_writes"], true_negative=proof)
        graded = self.save_scenario_result(scenarios.result(item, "NO_INCIDENT", source="observer", **facts))
        allowed = graded["matched_expected"] or graded["score"] == "acceptable"
        return item["expected"]["incident"] is not True and allowed and proof["passed"]

    def true_negative(self):
        observations = sorted(self.directory.glob("primary-*-observation.json"))
        stamps, complete = [], 0
        for path in observations:
            observation = json.loads(path.read_text(encoding="utf-8"))
            stamps.append(observation["captured_at"])
            complete += observation.get("telemetry", {}).get("thresholds_complete") is True
        with SQLiteExecutionLedger(self.directory / "executor.sqlite") as ledger:
            executor_actions = ledger.db.execute("SELECT COUNT(*) FROM executor_actions").fetchone()[0]
        incidents = self.store.db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        actions = self.store.db.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
        proof = true_negative_proof(
            injected=self.chaos_record is not None, observations=stamps, window_seconds=120,
            thresholds_complete=complete, coordinator_incidents=incidents, coordinator_actions=actions,
            executor_actions=executor_actions, template_sha256=fingerprint(self.deployment()["spec"]["template"]),
            baseline_sha256=self.baseline["template_sha256"])
        save(self.directory / "true-negative.json", proof)
        return proof

    def recover_incident(self, incident):
        try:
            if incident is not None:
                if self.chaos_fault == "pod":
                    # The 2x2 matrix's off-diagonal cell during the fault: a rollback changes nothing.
                    self.forced_refusal(incident, "ROLLBACK", FORCED[("pod", "ROLLBACK")], "forced-rollback")
                if self.agent_mode:
                    super().recover_incident(incident)
                else:
                    self.rule_recovery(incident)
                if self.chaos_fault == "cpu":
                    self.forced_refusal(incident, "RESTART", FORCED[("cpu", "RESTART")], "forced-restart")
            elif self.chaos_fault == "cpu":
                # N3 scored NO_INCIDENT (true-negative proof already saved): the CPU cell still runs under the
                # stress, against a harness-made incident id as in the drift cell.
                synthetic = {"incident_id": "INC-FORCED-CPU-" + uuid4().hex[:12].upper()}
                self.forced_refusal(synthetic, "RESTART", FORCED[("cpu", "RESTART")], "forced-restart")
        finally:
            self.remove_chaos()
        self.check("Chaos Mesh object deleted and its cleanup confirmed", chaos=self.chaos_record and
                   self.chaos_record["name"])

    def after_recovery_scenarios(self):
        if self.chaos_fault == "pod":
            self.drift_refusal()

    def approve_rule(self, proposal, decision):
        """Approval for a rule-mode repair through this run's session; none when the label expects no repair."""
        if self.expect in ("escalated", "no-incident"):
            return None
        return self.request_approval(proposal, decision)

    def rule_recovery(self, incident):
        """Plan with the code rule provider and a trusted live-template read, then the guarded path."""
        folder = self.directory / PLANNING_FOLDER
        planning = {"baseline_reader": lambda current: [self.baseline], "retriever": retrieve_runbooks,
                    "provider": mock_provider,
                    "template_reader": lambda current: self.deployment()["spec"]["template"]}
        self.summary.update(planning="Code rule provider with a trusted live-template read; no model call",
                            expected_outcome=self.expect)
        try:
            state = run_durable_case(self, incident, folder=folder, planning=planning, approve=self.approve_rule)
        except Exception:
            # A failure after planning still scores the decision that was delivered.
            self.score_rule(incident, folder)
            raise
        graded = self.score_rule(incident, folder, state)
        require(graded is not None, "Rule planning delivered no proposal")
        # A no-incident label (T1) expects no Coordinator state at all, so any planned incident fails it.
        expected = {"recovered": "RECOVERED", "escalated": "NEEDS_REVIEW"}.get(self.expect, "no incident")
        require(state == expected, f"Chaos recovery stopped at {state}; expected {expected}")
        require(graded["matched_expected"], f"Scenario {graded['scenario']} scored {graded['score']}: decision "
                f"{graded['actual']}; pre-registered correct {graded['expected']['correct']}")
        if graded["actual"] == "RESTART":
            self.attribute(folder)
        self.check(f"Scenario {graded['scenario']} decided {graded['actual']} by the code rule; "
                   f"Coordinator {state}", action_id=graded.get("action_id"))

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

    def target_pods(self):
        pods = json.loads(self.kubectl("get", "pods", "-l", "app=" + self.name, "-o", "json").stdout)["items"]
        return [{"name": pod["metadata"]["name"], "uid": pod["metadata"]["uid"]} for pod in pods
                if not pod["metadata"].get("deletionTimestamp")]

    def attribute(self, folder):
        """The attribution proof for a verified RESTART; the run fails when it does not hold."""
        outcome = json.loads((folder / "outcome.json").read_text(encoding="utf-8"))
        current = self.target_pods()
        raw = self.chaos_cluster().call("get", self.chaos_record["kind"], self.chaos_record["name"],
                                        "--ignore-not-found=true", "-o", "json")
        chaos_now = json.loads(raw) if raw.strip() else None
        proof = attribution(self.chaos_record, outcome, current_pods=current, chaos_now=chaos_now)
        save(self.directory / "attribution.json", proof)
        self.summary["attribution"] = {"attributed": proof["attributed"],
                                       "failed": [item["name"] for item in proof["checks"] if not item["passed"]]}
        require(proof["attributed"], "RECOVERED cannot be attributed to the RESTART: "
                + ", ".join(self.summary["attribution"]["failed"]))
        self.check("RESTART attributed: new Pod not selected by the chaos, recovered before its expiry, "
                   "chaos still running", seconds_before_expiry=proof["checks"][2]["detail"]["seconds_before_expiry"])

    def forced_proposal(self, incident, action, incident_id=None):
        """A harness-forced repair proposal; RESTART parameters are bound exactly as the Planner binds them."""
        incident_id = incident_id or incident["incident_id"]
        parameters = (restart_parameters(incident_id, self.baseline) if action == "RESTART" else
                      {key: self.baseline[key] for key in ("baseline_id", "template_sha256")})
        health = (self.baseline.get("health_evidence") or {}).get("evidence_id")
        return {"schema_version": "1.0", "kind": "proposal", "incident_id": incident_id,
                "action_id": "ACT-FORCED-" + uuid4().hex, "target": deepcopy(self.target), "action": action,
                "parameters": parameters,
                "based_on_resource_version": self.deployment()["metadata"]["resourceVersion"],
                "rationale": "Harness-forced wrong action for the negative matrix; the Guard must refuse it.",
                "evidence_refs": [incident_id, *([health] if health else [])]}

    def forced_refusal(self, incident, action, expected_code, label, *, incident_id=None):
        """The restricted Executor must refuse a forced wrong action with 0 writes.

        The action carries no approval (approval.json is null): only the Guard's
        own check can refuse it, and a check that unexpectedly passes escalates
        for an operator instead of writing. No Coordinator route is made: the
        real incident's state is untouched.

        A rollout still in progress (the drift cell's) can change the
        Deployment's resourceVersion between the proposal's read and the
        Executor's. The Executor then answers CONFLICT before any Guard check,
        so nothing was refused or decided. Only that answer, with no Guard
        result and no write, is retried with a fresh proposal, at most
        FORCED_STALE_RETRIES more times; each stale attempt is kept in the proof.
        """
        folder = self.directory / label
        folder.mkdir()
        stale = []
        for attempt in range(FORCED_STALE_RETRIES + 1):
            proposal = self.forced_proposal(incident, action, incident_id)
            before = self.deployment()
            dependencies = self.dependency_result(proposal)
            # No approval for a wrong action: a REJECT still outranks the missing approval, and anything else escalates.
            for name, value in (("proposal.json", proposal), ("dependencies.json", dependencies),
                                ("approval.json", None)):
                save(folder / name, value)
            adapters = self.adapters(folder)
            try:
                started = adapters.cluster.stats["write_attempts"]
                result = execute(proposal, adapters)
                entry = adapters.ledger.get(proposal["action_id"])
                stats = {"write_attempts": adapters.cluster.stats["write_attempts"] - started}
            finally:
                adapters.ledger.close()
            after = self.deployment()
            guard = (entry or {}).get("guard_result") or {}
            unchanged = fingerprint(before["spec"]["template"]) == fingerprint(after["spec"]["template"])
            if (result["status"] == "CONFLICT" and not guard and stats["write_attempts"] == 0 and unchanged
                    and result.get("resource_version") != proposal["based_on_resource_version"]
                    and attempt < FORCED_STALE_RETRIES):
                stale.append({"action_id": proposal["action_id"], "executor_status": result["status"],
                              "based_on_resource_version": proposal["based_on_resource_version"],
                              "resource_version_at_executor": result.get("resource_version"),
                              "details": result.get("details")})
                save(folder / f"stale-proposal-{attempt + 1}.json", proposal)
                time.sleep(FORCED_STALE_WAIT_SECONDS)
                continue
            break
        proof = {"label": label, "action": action, "expected_reason": expected_code,
                 "action_id": proposal["action_id"], "executor_status": result["status"],
                 "guard_verdict": guard.get("verdict"), "guard_reason_codes": guard.get("reason_codes", []),
                 "executor_patch_attempts": stats.get("write_attempts"),
                 "executor_journal_attempts": (entry or {}).get("attempts"),
                 "template_unchanged": unchanged,
                 "generation_unchanged": before["metadata"]["generation"] == after["metadata"]["generation"],
                 "stale_resource_version_attempts": stale}
        save(folder / "proof.json", proof)
        self.summary.setdefault("forced_refusals", []).append(proof)
        require(proof["executor_patch_attempts"] == 0 and proof["executor_journal_attempts"] == 0
                and proof["template_unchanged"] and proof["generation_unchanged"],
                f"Forced {action} attempted a write or changed the Deployment")
        require(result["status"] == "FAILED" and proof["guard_verdict"] == "REJECT"
                and expected_code in proof["guard_reason_codes"],
                f"Forced {action} was not refused with {expected_code}: {proof['guard_verdict']} "
                f"{proof['guard_reason_codes']}")
        self.check(f"Forced {action} refused by the Guard with {expected_code}; zero Executor writes",
                   action_id=proposal["action_id"])
        return proof

    def settle_deployment(self):
        """Wait, at most DRIFT_SETTLE_SECONDS, until two reads DRIFT_SETTLE_POLL_SECONDS apart show the same
        resourceVersion and the controller has observed the current generation.

        The drift's rollout keeps updating the Deployment for a while; a forced
        proposal read during that time can go stale before the Executor reads
        it. Not settling is recorded, not fatal: forced_refusal still retries a
        stale read, and a version that keeps moving still fails the cell.
        """
        started = time.monotonic()
        previous = self.deployment()
        reads = 1
        while True:
            time.sleep(DRIFT_SETTLE_POLL_SECONDS)
            current = self.deployment()
            reads += 1
            metadata, status = current["metadata"], current.get("status") or {}
            observed = status.get("observedGeneration")
            if (metadata["resourceVersion"] == previous["metadata"]["resourceVersion"]
                    and (observed is None or observed == metadata["generation"])):
                return {"settled": True, "reads": reads, "resource_version": metadata["resourceVersion"]}
            if time.monotonic() - started >= DRIFT_SETTLE_SECONDS:
                return {"settled": False, "reads": reads, "resource_version": metadata["resourceVersion"]}
            previous = current

    def drift_refusal(self):
        """The drift cell: inject R1's template fault, refuse a forced RESTART (0 writes), then restore."""
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=180s", timeout=200)
        drift = scenarios.scenario("R1")
        injection = drift["injection"]
        self.kubectl(*scenarios.set_env_argv(self.name, injection["container"], injection["env"], injection["value"]))
        incident = {"incident_id": "INC-FORCED-DRIFT-" + uuid4().hex[:12].upper()}
        try:
            self.summary["drift_settle"] = self.settle_deployment()
            self.forced_refusal(incident, "RESTART", FORCED[("drift", "RESTART")], "forced-restart-drift")
        finally:
            self.administrator_restore("drift-restore-patch.json")
        self.kubectl("rollout", "status", "deployment/" + self.name, "--timeout=180s", timeout=200)
        self.check("Drift removed by the administrator; the target is back at its baseline")

    def administrator_restore(self, name, attempts=3):
        """Put the baseline template back as the lab administrator (conditional JSON Patch); not a Sentinel repair.

        The drift's rollout is still running, so a controller status update can
        change the resourceVersion between the read and the patch. Each attempt
        re-reads the Deployment and patches only that version of the same UID.
        """
        for attempt in range(1, attempts + 1):
            deployment = self.deployment()
            patch = [{"op": "test", "path": "/metadata/uid", "value": self.target["uid"]},
                     {"op": "test", "path": "/metadata/resourceVersion",
                      "value": deployment["metadata"]["resourceVersion"]},
                     {"op": "replace", "path": "/spec/template", "value": self.baseline["pod_template"]}]
            save(self.directory / name, patch)
            result = self.kubectl("patch", "deployment", self.name, "--type=json", "--patch-file",
                                  str(self.directory / name), allow_failure=True)
            if result.returncode == 0:
                self.summary["drift_cell_restored"] = True
                self.summary["drift_restore_attempts"] = attempt
                return
            time.sleep(1)
        require(False, f"The administrator could not restore the baseline after {attempts} conditional patches")


def main(argv=None):
    """install: Chaos Mesh and metrics-server into the connected lab (install only). preflight: read-only checks."""
    import argparse
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("command", choices=("install", "preflight"))
    parser.add_argument("--context", default=chaos.CONNECTED_CONTEXT,
                        help="Must be " + chaos.CONNECTED_CONTEXT + "; anything else is refused")
    parser.add_argument("--namespace", default=chaos.CONNECTED_NAMESPACE)
    parser.add_argument("--metrics-server-digest",
                        help="install: the metrics-server " + METRICS_SERVER_TAG + " image digest (sha256:<64 hex>)")
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--helm", default="helm")
    parser.add_argument(lab_lock.BREAK_OPTION, action="store_true",
                        help="install: remove a heavy-lab lock whose process is no longer running, then take it")
    args = parser.parse_args(argv)
    try:
        chaos.require_connected(args.context, args.namespace)
        if args.break_stale_lock and args.command != "install":
            raise ValueError(lab_lock.BREAK_OPTION + " applies only to install; preflight only reads")
        if args.command == "install" and not (args.metrics_server_digest
                                              and _DIGEST.fullmatch(args.metrics_server_digest)):
            raise ValueError("install needs --metrics-server-digest sha256:<64 hex>")
    except ValueError as error:
        parser.error(str(error))
    cluster = chaos.Cluster(args.context, args.namespace, kubectl=args.kubectl)
    if args.command == "install":
        def install():
            chaos.ensure_chaos_mesh(cluster, args.helm)
            ensure_metrics_server(cluster, args.metrics_server_digest, args.helm)
            print(f"Installed Chaos Mesh {chaos.CHAOS_VERSION} (namespace-filtered; {args.namespace} opted in) and "
                  f"metrics-server chart {METRICS_SERVER_CHART} into {args.context}", flush=True)
            return 0
        # The install changes the connected lab, so it holds the heavy-lab lock shared with P4.01 and P4.05.
        return lab_lock.run_holding(lab_lock.HeavyLabLock(holder="P4.03 chaos_recovery install",
                                                          break_stale=args.break_stale_lock), install)
    print(json.dumps(preflight(cluster), indent=2, sort_keys=True), flush=True)
    return 0
