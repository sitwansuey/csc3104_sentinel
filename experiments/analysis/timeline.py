"""Per-stage latency of one Boutique recovery run, from its files only.

A run is read from one or more directories in priority order: usually its
evidence bundle first, then the original run folder for files a bundle does not
keep (planning-progress.jsonl). Every stage records whether its timestamps came
from the bundle or the run folder.

Only timing numbers, roles, statuses, model tags and token counts are copied.
Prompts, messages, generated text, rationales and log lines are never read into
the output: every output key is written by this module.

Stages (seconds):
  detection     start -> incident observed. The start is fault_injected_at when
                the harness recorded it, else the timeline 'fault_injected'
                phase, else the first failing Observer snapshot (a lower bound,
                labelled 'from_first_failing_snapshot'; never imputed).
  planning      incident observed -> last planning progress record (agent runs
                with planning-progress.jsonl only).
  model         sum of model call wall times (part of planning).
  guard         planning end -> Guard ALLOW verdict. Includes the dependency
                check and the lab's pre-authorised approval.
  decision      incident observed -> Guard ALLOW verdict (planning + Guard). In
                harness mode it also covers the scripted negative cases.
  execution     Guard verdict -> Executor result recorded.
  verification  Executor result -> end of the Verifier outcome window.
  repair        start -> Executor result recorded (fault to repair).
  total         start -> end of the Verifier outcome window.

A model call is cold when its load_duration_seconds is over one second. Calls
recorded before load durations were captured cannot be classified and are
counted as unclassified, never as warm.
"""

from datetime import datetime
import json
import math
from pathlib import Path
import statistics

COLD_LOAD_SECONDS = 1.0
SNAPSHOT_SOURCES = ("deployment_status", "pod_status")
STAGES = ("detection", "planning", "model", "guard", "decision", "execution", "verification", "repair", "total")
ACTION_FOLDER = "approved-rollback"
START_BASES = ("fault_injected_at", "timeline", "from_first_failing_snapshot")
BUNDLE, RUN_FOLDER = "bundle", "run_folder"


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def seconds_between(start, end):
    if start is None or end is None:
        return None
    return round((end - start).total_seconds(), 3)


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class Sources:
    """Read a relative path from the first directory that has it; remember which kind it was."""

    def __init__(self, directories):
        self.directories = [(Path(path), kind) for path, kind in directories]

    def find(self, relative):
        for directory, kind in self.directories:
            path = directory / relative
            if path.is_file():
                return path, kind
        return None, None

    def json(self, relative):
        path, kind = self.find(relative)
        if path is None:
            return None, None
        try:
            return json.loads(path.read_text(encoding="utf-8-sig")), kind
        except (OSError, ValueError):
            return None, None

    def jsonl(self, relative):
        path, kind = self.find(relative)
        if path is None:
            return None, None
        records = []
        try:
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                if line.strip():
                    try:
                        value = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(value, dict):
                        records.append(value)
        except OSError:
            return None, None
        return records, kind


def _phase(timeline, name, scenario):
    for step in timeline if isinstance(timeline, list) else []:
        if isinstance(step, dict) and step.get("phase") == name and step.get("scenario") == scenario:
            return parse_time(step.get("at"))
    return None


def _start(summary, incident, notes):
    """(time, basis) of the fault start, never guessed."""
    recorded = parse_time(summary.get("fault_injected_at"))
    if recorded:
        return recorded, "fault_injected_at"
    phase = _phase(summary.get("timeline"), "fault_injected", "primary")
    if phase:
        return phase, "timeline"
    snapshots = [parse_time(signal.get("at")) for signal in (incident or {}).get("signals") or []
                 if isinstance(signal, dict) and signal.get("source") in SNAPSHOT_SOURCES]
    snapshots = [value for value in snapshots if value]
    if snapshots:
        notes.append("no injection time recorded; detection measured from the first failing snapshot")
        return min(snapshots), "from_first_failing_snapshot"
    return None, None


def _calls(raw):
    calls = []
    for call in raw if isinstance(raw, list) else []:
        if not isinstance(call, dict):
            continue
        load = number(call.get("load_duration_seconds"))
        calls.append({"role": call.get("role") if isinstance(call.get("role"), str) else None,
                      "status": call.get("status") if isinstance(call.get("status"), str) else None,
                      "seconds": number(call.get("seconds")), "load_seconds": load,
                      "prompt_eval_seconds": number(call.get("prompt_eval_duration_seconds")),
                      "eval_seconds": number(call.get("eval_duration_seconds")),
                      "prompt_tokens": call.get("prompt_tokens") if type(call.get("prompt_tokens")) is int else None,
                      "output_tokens": call.get("output_tokens") if type(call.get("output_tokens")) is int else None,
                      "cold": None if load is None else load > COLD_LOAD_SECONDS})
    return calls


def run_timeline(directories, *, run=None, bundle_id=None, planning_mode=None):
    """One ledger row. 'directories' is [(path, 'bundle' | 'run_folder'), ...] in priority order."""
    sources = Sources(directories)
    notes = []
    summary, _ = sources.json("summary.json")
    summary = summary if isinstance(summary, dict) else {}
    if not summary:
        notes.append("summary.json missing")
    incident, incident_kind = sources.json("primary-incident.json")
    incident = incident if isinstance(incident, dict) else None
    calls_raw, calls_kind = sources.json(ACTION_FOLDER + "/model-calls.json")
    progress, progress_kind = sources.jsonl(ACTION_FOLDER + "/planning-progress.jsonl")
    guard, guard_kind = sources.json(ACTION_FOLDER + "/initial-guard.json")
    execution, execution_kind = sources.json(ACTION_FOLDER + "/execution.json")
    outcome, outcome_kind = sources.json(ACTION_FOLDER + "/outcome.json")
    guard = guard if isinstance(guard, dict) else {}
    execution = execution if isinstance(execution, dict) else {}
    outcome = outcome if isinstance(outcome, dict) else {}
    summary_kind = sources.find("summary.json")[1]
    timeline = summary.get("timeline")

    start, basis = _start(summary, incident, notes)
    start_kind = incident_kind if basis == "from_first_failing_snapshot" else summary_kind
    incident_at = parse_time((incident or {}).get("observed_at"))
    incident_at_kind = incident_kind
    if incident_at is None:
        incident_at, incident_at_kind = _phase(timeline, "incident_observed", "primary"), summary_kind
    planning_end, planning_kind = None, None
    if progress:
        moments = [parse_time(record.get("at")) for record in progress]
        moments = [moment for moment in moments if moment]
        failed = any(record.get("status") == "failed" for record in progress)
        if moments and not failed:
            planning_end, planning_kind = max(moments), progress_kind
    if planning_end is None:
        saved = _phase(timeline, "proposal_saved", ACTION_FOLDER)
        if saved:
            planning_end, planning_kind = saved, summary_kind
    guard_at, guard_at_kind = None, None
    if guard.get("verdict") == "ALLOW":
        guard_at, guard_at_kind = parse_time(guard.get("checked_at")), guard_kind
    if guard_at is None:
        guard_at, guard_at_kind = _phase(timeline, "guard_decision", ACTION_FOLDER), summary_kind
    applied_at = parse_time(execution.get("recorded_at")) if execution.get("status") == "APPLIED" else None
    applied_kind = execution_kind
    if applied_at is None and execution.get("status") in (None, "APPLIED"):
        applied_at, applied_kind = _phase(timeline, "execution_recorded", ACTION_FOLDER), summary_kind
    verified_at, verified_kind = parse_time(outcome.get("window_end")), outcome_kind

    calls = _calls(calls_raw)
    if calls_raw is not None and planning_mode is None:
        planning_mode = "agent"
    planning_mode = planning_mode or ("agent" if summary.get("model") else "harness")
    model_seconds = [call["seconds"] for call in calls if call["seconds"] is not None]

    def stage(start_at, start_from, end_at, end_from):
        value = seconds_between(start_at, end_at)
        if value is None:
            return None, None
        if value < 0:
            notes.append("negative interval ignored")
            return None, None
        return value, RUN_FOLDER if RUN_FOLDER in (start_from, end_from) else BUNDLE

    values = {
        "detection": stage(start, start_kind, incident_at, incident_at_kind),
        "planning": stage(incident_at, incident_at_kind, planning_end, planning_kind),
        "model": ((round(sum(model_seconds), 3), calls_kind) if model_seconds else (None, None)),
        "guard": stage(planning_end, planning_kind, guard_at, guard_at_kind),
        "decision": stage(incident_at, incident_at_kind, guard_at, guard_at_kind),
        "execution": stage(guard_at, guard_at_kind, applied_at, applied_kind),
        "verification": stage(applied_at, applied_kind, verified_at, verified_kind),
        "repair": stage(start, start_kind, applied_at, applied_kind),
        "total": stage(start, start_kind, verified_at, verified_kind),
    }
    if planning_mode == "harness" and values["decision"][0] is not None:
        notes.append("harness decision stage includes the scripted negative cases before the approved rollback")

    status = summary.get("status") if isinstance(summary.get("status"), str) else None
    outcome_status = outcome.get("status") if isinstance(outcome.get("status"), str) else None
    if status == "PASS":
        failure_stage = None
    elif summary.get("agent_failure"):
        failure_stage = "planning"
    elif incident_at is None:
        failure_stage = "detection"
    elif guard_at is None:
        # Older agent runs did not record agent_failure; no proposal means planning stopped.
        no_proposal = sources.find(ACTION_FOLDER + "/proposal.json")[0] is None
        failure_stage = "planning" if planning_mode == "agent" and no_proposal else "decision"
    elif applied_at is None:
        failure_stage = "execution"
    elif outcome_status != "RECOVERED":
        failure_stage = "verification"
    else:
        failure_stage = "after_recovery"
    if failure_stage == "planning":
        # Planning did not finish, so it has no planning or model duration. Its calls stay listed.
        values["planning"] = values["model"] = (None, None)
    failure = summary.get("agent_failure") if isinstance(summary.get("agent_failure"), dict) else {}
    return {
        "run": run, "bundle_id": bundle_id, "planning_mode": planning_mode,
        "model": summary.get("model") if isinstance(summary.get("model"), str) else None,
        "status": status, "outcome": outcome_status, "failure_stage": failure_stage,
        "planning_seconds_before_failure": number(failure.get("seconds")),
        "start_basis": basis,
        "stages": {name: values[name][0] for name in STAGES},
        "stage_sources": {name: values[name][1] for name in STAGES},
        "calls": calls,
        "schema_notes": sorted(set(notes)),
    }


def nearest_rank(ordered, fraction):
    return ordered[math.ceil(fraction * len(ordered)) - 1]


def describe(values):
    """n, median, p90 and p95 (nearest rank), min and max; n=0 gives nulls, never zeros."""
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return {"n": 0, "median": None, "p90": None, "p95": None, "min": None, "max": None}
    return {"n": len(ordered), "median": round(statistics.median(ordered), 3),
            "p90": nearest_rank(ordered, 0.90), "p95": nearest_rank(ordered, 0.95),
            "min": ordered[0], "max": ordered[-1]}


def temperature(row):
    """'cold' if any call loaded the model, 'warm' if every call is known warm, else 'unclassified'."""
    flags = [call["cold"] for call in row["calls"]]
    if any(flag is True for flag in flags):
        return "cold"
    if flags and all(flag is False for flag in flags):
        return "warm"
    return "unclassified"


def aggregate(rows):
    """Per planning mode: run counts, stage summaries, and model time split by cold/warm."""
    result = {}
    for mode in sorted({row["planning_mode"] for row in rows}):
        members = [row for row in rows if row["planning_mode"] == mode]
        failures = {}
        for row in members:
            if row["failure_stage"]:
                failures[row["failure_stage"]] = failures.get(row["failure_stage"], 0) + 1
        bases = {}
        for row in members:
            if row["stages"]["detection"] is not None:
                bases[row["start_basis"]] = bases.get(row["start_basis"], 0) + 1
        # A failed call returns no answer; its short wall time is not a model latency sample.
        calls = [call for row in members for call in row["calls"] if call["status"] != "failed"]
        result[mode] = {
            "runs": len(members),
            "passed": sum(row["status"] == "PASS" for row in members),
            "failure_stages": dict(sorted(failures.items())),
            "detection_start_basis": dict(sorted(bases.items())),
            "stages": {name: describe(row["stages"][name] for row in members) for name in STAGES},
            "model_by_temperature": {name: describe(row["stages"]["model"] for row in members
                                                    if temperature(row) == name)
                                     for name in ("warm", "cold", "unclassified")},
            "calls_by_temperature": {name: describe(call["seconds"] for call in calls if call["cold"] is flag)
                                     for name, flag in (("warm", False), ("cold", True), ("unclassified", None))},
            "cold_load": describe(call["load_seconds"] for call in calls if call["cold"] is True),
        }
    return result
