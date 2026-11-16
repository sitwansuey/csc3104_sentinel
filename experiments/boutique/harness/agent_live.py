"""Real RCA and model planning for the existing guarded Boutique recovery path."""
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd

from agent.llm import Ollama
from agent.untrusted import scrub, sha256, suspicious
from services.rca.core import DEFAULT_CONFIG
from services.guard.common import json_sha256
from services.planner.agent_provider import AgentPlanner, names_target, planning_budget
from services.planner.store import PlannerStore
from services.planner.taxonomy import components, policy as decision_policy
from experiments.boutique.harness import scenarios, targets
from experiments.boutique.harness.durable_case import run_durable_case
from experiments.boutique.harness.scoring import ScenarioScoringMixin
from experiments.recovery.live import require, save

__all__ = ["AgentPlanningMixin", "export_telemetry", "names_target"]
LIVE_KEEP_ALIVE = "30m"
# Evidence and dashboard checks read the agent case from this folder.
PLANNING_FOLDER = "approved-rollback"
# Longest cleaned log message kept in logs.csv; the raw line stays in the observation.
MESSAGE_LIMIT = 2000
# The reviewed Boutique topology: its nodes are the diagnosis's component vocabulary.
TOPOLOGY = Path(__file__).resolve().parents[3] / "deploy/boutique/dependencies.json"


def load_topology():
    """The reviewed topology; ValueError unless its nodes are a valid component vocabulary."""
    topology = json.loads(TOPOLOGY.read_text(encoding="utf-8"))
    nodes = topology.get("nodes") if isinstance(topology, dict) else None
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("The reviewed topology must list its nodes: " + TOPOLOGY.name)
    components(nodes[0], topology)
    return topology


def csv_cell(text):
    """Keep a spreadsheet from reading a cleaned message as a formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def _time(value):
    stamp = pd.Timestamp(value)
    if stamp.tzinfo is None:
        raise ValueError("Telemetry timestamps must include a timezone")
    return stamp.astimezone(timezone.utc)


def export_telemetry(directory, target, destination, *, incident=None, lookback_seconds=600):
    """Project saved live Observer snapshots into the agent's CSV interface."""
    if type(lookback_seconds) is not int or lookback_seconds <= 0:
        raise ValueError("lookback_seconds must be a positive integer")
    if incident and incident["target"] != target:
        raise ValueError("Incident telemetry belongs to another target")
    destination.mkdir(parents=True, exist_ok=False)
    metrics, logs = set(), {}
    snapshots = []
    untimed = {}
    skipped = duplicates = raw_metrics = raw_logs = 0
    counts = {"suspicious_lines": 0, "truncated_lines": 0, "control_chars_removed": 0}
    end = _time(incident["observed_at"]) if incident else None
    start = min([end, *(_time(s["at"]) for s in incident["signals"])]) - timedelta(
        seconds=lookback_seconds) if incident else None
    def in_window(at):
        return start is None or start <= _time(at) <= end

    def add_log(at, message, source, identity, time_known=True):
        nonlocal duplicates, raw_logs
        if not in_window(at):
            return
        raw_logs += 1
        # Identity includes the emitting Pod/container or Event object. A repeated
        # poll is not another occurrence, but equal messages from two Pods are.
        key = (identity, at, message, source)
        if key in logs:
            duplicates += 1
            return
        # The message is workload text: the CSV gets a cleaned, bounded copy,
        # the hash of the raw line and the rule ids it matched. The raw line
        # stays in the saved observation.
        cleaned, removed, truncated = scrub(message, MESSAGE_LIMIT)
        rules = suspicious(message)
        counts["control_chars_removed"] += removed
        counts["truncated_lines"] += truncated
        counts["suspicious_lines"] += bool(rules)
        logs[key] = [at, target["name"], csv_cell(cleaned), source, identity, time_known,
                     sha256(message), ";".join(rules)]

    for path in sorted(directory.glob("*-observation.json")):
        observation = json.loads(path.read_text(encoding="utf-8"))
        timestamp = observation["captured_at"]
        if not in_window(timestamp):
            skipped += 1
            continue
        deployment = observation["deployment"]
        if (observation["cluster_id"] != target["cluster_id"]
                or deployment["metadata"]["uid"] != target["uid"]):
            raise ValueError("Observer telemetry belongs to another target")
        timestamp = _time(timestamp).isoformat()
        snapshots.append(path.name)
        for key, value in {"desired_replicas": deployment["spec"].get("replicas", 1),
                           "ready_replicas": deployment.get("status", {}).get("readyReplicas", 0),
                           "available_replicas": deployment.get("status", {}).get("availableReplicas", 0)}.items():
            raw_metrics += 1
            metrics.add((timestamp, target["name"], key, value))
        for entry in observation.get("logs", []):
            identity = "/".join(str(entry.get(k, "")) for k in ("pod_uid", "pod", "container"))
            for line in entry["text"].splitlines():
                prefix, _, message = line.partition(" ")
                # kubectl logs --timestamps returns RFC3339 followed by the application line.
                try:
                    prefix = _time(prefix).isoformat()
                except ValueError:
                    # Untimestamped overlapping tails cannot establish occurrence
                    # counts. Keep one copy per emitting container, marked unknown.
                    prefix, message = "", line
                if prefix:
                    add_log(prefix, message, "container", identity)
                else:
                    at = untimed.setdefault((identity, line), timestamp)
                    add_log(at, message, "container_untimed", identity, False)
        for event in observation.get("events", []):
            message = event.get("message", event.get("reason", ""))
            if message:
                at = event.get("lastTimestamp") or event.get("eventTime")
                identity = event.get("uid") or "/".join(str(event.get(k, "")) for k in (
                    "involved_uid", "reason", "type"))
                # Legacy events without occurrence time have unknown timing. Do
                # not make them look like a fresh warning at every collection.
                if not at:
                    identity += "/" + json_sha256(str(message))
                    at = untimed.setdefault(("event", identity), timestamp)
                add_log(_time(at).isoformat(), str(message), "kubernetes_event", identity,
                        bool(event.get("lastTimestamp") or event.get("eventTime")))
    require(snapshots and metrics, "Live Observer snapshots are required for agent diagnosis")
    for name, header, rows in [
            ("metrics.csv", ["timestamp", "cmdb_id", "kpi_name", "value"], sorted(metrics)),
            ("logs.csv", ["timestamp", "cmdb_id", "message", "source", "source_id", "timestamp_known",
                          "message_sha256", "suspicious_rules"], sorted(logs.values()))]:
        with (destination / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)
    save(destination.parent / "telemetry-export.json", {
        "source": "live Observer snapshots", "snapshots": snapshots, "metrics": len(metrics),
        "log_lines": len(logs), "duplicate_metrics_removed": raw_metrics - len(metrics),
        "duplicate_logs_removed": duplicates, "input_log_lines": raw_logs,
        "snapshots_outside_window": skipped, **counts,
        "window_start": start.isoformat() if start else None,
        "window_end": end.isoformat() if end else None, "traces_available": False})


class AgentPlanningMixin(ScenarioScoringMixin):
    agent_config = None
    expect = "recovered"
    # Whether the scenario score decides the run's result. False only for an explicit
    # --expect that differs from the scenario's label (the R1 planning-failure check).
    scenario_gates_pass = True

    def load_agent_config(self):
        config = json.loads(DEFAULT_CONFIG.read_text(encoding="utf-8"))
        # Keep the warmed model loaded through baseline, injection and detection.
        # Ollama's own default unloads it after 5 minutes. -AgentConfig can override.
        config["keep_alive"] = LIVE_KEEP_ALIVE
        if self.agent_config:
            config.update(json.loads(self.agent_config.read_text(encoding="utf-8")))
        config["allow_abstention"] = True
        # Record the planning mode up front so a failed run is still identifiable.
        self.summary.update(planning="Live RCA agent and Ollama Recovery Planner", model=config["model"],
                            decision_policy=config.get("decision_policy", "gated"),
                            scope=f"Online Boutique {getattr(self, 'name', None) or targets.default_target()}; real Observer/Coordinator/Agent/Guard/Executor/Verifier")
        return config

    def prepare(self):
        # Check and load the model before any cluster change, so a missing or
        # cold model stops the run before a fault is injected.
        self.prepare_model()
        super().prepare()

    def prepare_model(self):
        """Preflight and warm up Ollama, then record its runtime in model-runtime.json."""
        stage = "preflight"
        started = time.monotonic()
        try:
            config = self.load_agent_config()
            # The Planner would refuse a bad budget, decision policy or topology only after the fault is injected.
            planning_budget(config)
            decision_policy(config)
            load_topology()
            llm = Ollama(config)
            llm.preflight()
            stage = "warm_up"
            warm_up = llm.warm_up()
        except Exception as error:
            reason = " ".join(str(error).split())[:500]
            self.summary["agent_failure"] = {"stage": stage, "error": type(error).__name__ + ": " + reason,
                                             "fault_injected": False,
                                             "seconds": round(time.monotonic() - started, 1)}
            self.event(3, "error", "Agent model check failed; no fault injected: " + reason[:300],
                       progress={"stage": stage, "status": "failed"})
            raise
        runtime = llm.runtime_info()
        save(self.directory / "model-runtime.json", {
            "captured_at": datetime.now(timezone.utc).isoformat(), **runtime, "warm_up": warm_up})
        load = max(call["load_duration_seconds"] for call in warm_up)
        self.event(3, "status", "Agent model is loaded and ready", progress={
            "stage": "warm_up", "status": "completed", "load_duration_seconds": load,
            "seconds": round(time.monotonic() - started, 2)})

    def recover_incident(self, incident):
        """Plan through the durable Planner journal, then the normal guarded recovery.

        Diagnosis, the Recovery Planner call and rebinding are one journaled,
        bounded attempt. Every planning failure ends in a saved, delivered
        ESCALATE that moves the incident to NEEDS_REVIEW; nothing falls back to
        a harness repair. A restart with the same run folder replays a saved
        proposal without another model call.

        A catalog scenario run scores the delivered decision in
        scenario-result.json before any pass or fail check, and also when a later
        step (Guard, Executor or Verifier) fails.
        """
        if incident is None:
            return  # A scored NO_INCIDENT that the scenario's label allows; nothing to plan.
        config = self.load_agent_config()
        # The evidence summarizer reads this to keep an expected escalation out
        # of the RECOVERED column, since both end with status PASS.
        variant = getattr(self, "outcome_variant", None)
        self.summary["expected_outcome"] = variant or self.expect
        folder = self.directory / PLANNING_FOLDER
        folder.mkdir(exist_ok=True)
        planner = None

        def progress(update):
            # This stream is safe operational metadata: never raw prompts,
            # application logs, generated text or hidden model reasoning.
            with (folder / "planning-progress.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(), **update}) + "\n")
            save(folder / "model-calls.json", planner.calls)
            label = update.get("role") or update.get("tool") or update["stage"]
            elapsed = f" ({update['seconds']:.0f}s)" if "seconds" in update else ""
            self.event(3, "heartbeat" if update["status"] == "running" else
                       "error" if update["status"] == "failed" else "status",
                       f"{label}: {update['status']}{elapsed}", target=self.target,
                       incident_id=incident["incident_id"], progress=update)

        planner = AgentPlanner(config, folder=folder, baseline=self.baseline,
                               telemetry=lambda destination, current: export_telemetry(
                                   self.directory, self.target, destination, incident=current),
                               deployment_reader=lambda target: self.deployment(), progress=progress,
                               topology=load_topology())
        self.event(3, "status", "Agent is diagnosing live Observer telemetry", target=self.target)
        started = time.monotonic()
        try:
            state = run_durable_case(self, incident, folder=folder, planning=planner.options(),
                                     approve=self.approve_planned)
        except Exception:
            # A failure after planning still scores the decision that was delivered.
            self.score_delivered(incident, folder, planner)
            raise
        finally:
            save(folder / "model-calls.json", planner.calls)
            self.summary["agent_model_calls"] = len(planner.calls)
            if planner.finding:
                # Rule ids and counts from the code-owned injection gate, never the flagged text.
                self.summary["injection_finding"] = planner.finding
            if planner.decision:
                # Policy, eligibility codes and whether a code rule or the model decided.
                self.summary["planning_decision"] = planner.decision
        with PlannerStore(folder / "planner.db") as journal:
            record = journal.get(incident["incident_id"])
        proposal = record["proposal"] if record else None
        if proposal:
            save(folder / "model-proposal.json", proposal)
            self.summary["planning_journal"] = {key: record[key] for key in ("attempts", "error_code", "delivered")}
        if proposal and proposal["action"] == "ESCALATE":
            self.planning_stopped(incident, record, planner, state, started)
        graded = self.score_scenario(incident, state, proposal, record, folder, planner)
        # A refused approval leaves the incident AWAITING_APPROVAL with no Executor request.
        require(state != "AWAITING_APPROVAL" or self.expect == "escalated",
                "Operator did not approve " + (proposal or {}).get("action_id", "the proposal")
                + "; no Executor write attempted")
        expected = ({"unhealthy": "FAILED", "unavailable-evidence": "NEEDS_REVIEW"}[variant] if variant else
                    "NEEDS_REVIEW" if self.expect == "escalated" else "RECOVERED")
        require(state == expected and (self.expect != "escalated" or proposal["action"] == "ESCALATE"),
                f"Agent recovery stopped at {state}; expected {expected}")
        if variant:
            require(proposal["action"] == "ROLLBACK"
                    and self.summary.get("outcome_expectation", {}).get("matched_expected") is True,
                    "Expected the lab variant's independently verified post-repair outcome")
        if graded is not None and self.scenario_gates_pass:
            require(graded["matched_expected"],
                    f"Scenario {graded['scenario']} scored {graded['score']}: decision {graded['actual']} "
                    f"from {graded['decision_source']}; pre-registered correct {graded['expected']['correct']}"
                    + (f", rule code {graded['expected']['rule_code']} matched {graded['rule_code_matched']}"
                       if "rule_code" in graded["expected"] else ""))
        if state == "NEEDS_REVIEW" and not variant:
            # The expected escalation is not a repair: the administrator removes the fault.
            require(self.emergency_restore(), "Administrator restore after the expected escalation failed")
            source = (graded or {}).get("decision_source")
            # A decided escalation is not a planning failure; say which one this was.
            name = (f"scenario escalation decided by the {source} ended in a saved ESCALATE and NEEDS_REVIEW"
                    if source in ("rule", "model") else
                    "agent planning failure ended in a saved ESCALATE and NEEDS_REVIEW")
            self.check(name, error_code=record["error_code"], attempts=record["attempts"])

    def score_scenario(self, incident, state, proposal, record, folder, planner):
        """Score the delivered decision against the scenario's pre-registered sets; None when unscored."""
        item = getattr(self, "scenario", None)
        if item is None:
            return None
        error_code = (record or {}).get("error_code")
        outcome = scenarios.decision(proposal, error_code)
        decision = planner.decision
        if decision is None and (folder / "planner.db").is_file():
            # A replayed proposal made no attempt in this process: read the last attempt's decision from the journal.
            with PlannerStore(folder / "planner.db") as journal:
                decision = (journal.attempt_usage(incident["incident_id"]) or {}).get("decision")
        decision = decision or {}
        facts = self.scenario_facts(incident, state, proposal["action_id"], folder)
        facts.update(planning_error_code=error_code,
                     decided_by=decision.get("decided_by"), model_action=decision.get("model_action"),
                     rule_overrode_model=bool(decision.get("overridden")),
                     eligibility_codes=list(decision.get("codes") or []))
        source = scenarios.decision_source(proposal, error_code, decided_by=facts["decided_by"],
                                           model_action=facts["model_action"])
        return self.save_scenario_result(scenarios.result(item, outcome, source=source, **facts))

    def score_delivered(self, incident, folder, planner):
        """Score a delivered proposal when a later step failed; None when unscored or nothing was delivered."""
        if getattr(self, "scenario", None) is None or not (folder / "planner.db").is_file():
            return None
        with PlannerStore(folder / "planner.db") as journal:
            record = journal.get(incident["incident_id"])
        current = self.store.incident(incident["incident_id"])
        if not record or not record["delivered"] or not record["proposal"] or current is None:
            return None
        return self.score_scenario(incident, current["state"], record["proposal"], record, folder, planner)

    def no_incident(self, label):
        """Score NO_INCIDENT; the run continues only when the scenario's label allows no incident."""
        item = getattr(self, "scenario", None)
        if item is None:
            return False
        self.summary["expected_outcome"] = self.expect
        graded = self.save_scenario_result(scenarios.result(
            item, "NO_INCIDENT", source="observer", **self.scenario_facts(None, None, None, self.directory)))
        return item["expected"]["incident"] is not True and graded["matched_expected"]

    def planning_stopped(self, incident, record, planner, state, started):
        """Record one visible stop: summary.json and the dashboard 'Agent planning stopped' event."""
        code = record["error_code"]
        reason = f"ESCALATE {code or 'proposed by the model'} after {record['attempts']} attempt(s)"
        # The cause text of the final attempt, if it had one (Ollama's own reason and host).
        cause = [item["error"] for item in planner.failures if item["attempt"] == record["attempts"]]
        if cause:
            reason += ": " + cause[-1]
        calls = planner.calls
        self.summary["agent_failure"] = {
            "error": reason[:600], "error_code": code, "attempts": record["attempts"], "incident_state": state,
            "model_calls_completed": sum(call.get("status") == "completed" for call in calls),
            "model_attempts": len(calls), "seconds": round(time.monotonic() - started, 1)}
        self.event(3, "error", "Agent planning stopped; no repair proposed: " + reason[:300],
                   target=self.target, incident_id=incident["incident_id"],
                   progress={"stage": "planning", "status": "failed"})

    def approve_planned(self, proposal, decision):
        """Approve the planned repair through this run's authenticated approval session.

        The approver (interactive, auto-lab or reject) creates the approval through
        approval_http, and summary.json records the strategy. An --expect escalated
        run asks for no approval at all: a repair proposal there is refused without
        starting the authority, so the run fails its expectation with zero writes.
        """
        if self.expect == "escalated":
            return None
        return self.request_approval(proposal, decision)

    # Agent E2E mode runs one real incident/recovery. The separate executor suite
    # retains race, duplicate, crash and redundant-repair scenarios.
    def negative_cases(self, incident):
        pass

    def conditional_race(self, incident):
        pass

    def crash_restart(self):
        pass

    def after_recovery_scenarios(self):
        pass
