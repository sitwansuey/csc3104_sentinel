"""Regenerate the results tables and claim values from committed evidence bundles.

Tables are written between '<!-- BEGIN GENERATED:<name> -->' and
'<!-- END GENERATED:<name> -->' markers in docs/results.md and evidence/README.md.
The latency tables come from the newest ledger under evidence/evaluation/latency/
(scripts/latency_report.py); --check recomputes its bundle-sourced values.
The limitations register (docs/limitations.json) is rendered into
docs/limitations.md and the README. A 'claim-<id>' block in the README, docs/
(and one folder below it) or deploy/ Markdown quotes that claim with its current value and scope caveat.
Withdrawn bundles are excluded. Failed and incomplete runs stay in every
denominator. With --check nothing is written: the command fails when a
generated block was hand-edited or is stale, when a bundle's files no longer
match its MANIFEST, when a claim is unsupported or its value is out of date, or
when a known-stale status phrase appears in the docs without a claim id beside it.
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
import difflib
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from experiments.analysis import timeline  # noqa: E402
from scripts import trace_view  # noqa: E402
from scripts.evidence import collect, limitations  # noqa: E402

TARGETS = {
    "docs/results.md": ("runs", "recovery-checks", "agent-runs", "scenario-runs", "guard-rejection", "guard-eval",
                        "executor-wedge", "timings", "latency", "model-calls", "chaos", "chaos-recovery",
                        "security-lab", "observer", "distributed-runs", "kind-lab-sessions", "executor-admission", "worker-container", "claims"),
    "evidence/README.md": ("index", "exclusions"),
    "docs/limitations.md": ("limitations",),
    "README.md": ("limitations-summary",),
}
# Markdown that may quote claims through 'claim-<lower-case claim id>' blocks.
QUOTE_SOURCES = ("README.md", "docs/*.md", "docs/*/*.md", "deploy/*.md", "deploy/*/*.md")
# Status phrases that went stale once live evidence existed. Each may appear
# only in a paragraph that also cites a claim id from evidence/claims.json.
STALE_PHRASES = ("still pending", "remains deferred", "no new protected live run", "pending rebuild/scan",
                 "no newly built/scanned image")
CLAIM_ID = re.compile(r"\bC-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
BLOCK = re.compile(r"(<!-- BEGIN GENERATED:(?P<name>[a-z0-9-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED:(?P=name) -->)",
                   re.DOTALL)
OUTCOMES = ("PASS", "FAILED", "INTERRUPTED", "INCOMPLETE")
NONE = "_No bundles yet._"
EXCLUSIONS = "evidence/exclusions.json"
# Classes whose runs may carry a catalog scenario result (scenario-result.json).
SCENARIO_CLASSES = ("agent", "chaos-recovery")
SECURITY_PROBES = ("invalid_quantity", "missing_client_certificate", "wrong_client_identity", "direct_service_bypass",
                   "direct_pod_ip_bypass", "direct_egress_bypass", "proxy_outage", "outage_direct_bypass")


@dataclass
class Bundle:
    entry: dict
    directory: Path
    manifest: dict

    @property
    def bundle_id(self):
        return self.entry["bundle_id"]

    @property
    def run_class(self):
        return self.entry["class"]

    def json(self, relative):
        if relative not in {item["path"] for item in self.manifest.get("files", [])}:
            return None
        return json.loads((self.directory / relative).read_text(encoding="utf-8"))

    def text(self, relative):
        if relative not in {item["path"] for item in self.manifest.get("files", [])}:
            return None
        return (self.directory / relative).read_text(encoding="utf-8")

    def jsonl(self, relative):
        """JSON object lines of a collected file, skipping any other line; None when the file is not collected."""
        text = self.text(relative)
        if text is None:
            return None
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


class EvidenceError(ValueError):
    """The evidence tree, a generated block or a claim is inconsistent."""


def load_index(root):
    path = Path(root) / "evidence/index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"bundles": []}


def load_bundles(root, *, include_withdrawn=False):
    bundles = []
    for entry in load_index(root)["bundles"]:
        if entry.get("status") == "withdrawn" and not include_withdrawn:
            continue
        directory = Path(root) / "evidence" / entry["path"]
        manifest_path = directory / "MANIFEST.json"
        if not manifest_path.exists():
            raise EvidenceError(f"Bundle {entry['bundle_id']} has no MANIFEST.json at evidence/{entry['path']}")
        bundles.append(Bundle(entry, directory, json.loads(manifest_path.read_text(encoding="utf-8"))))
    return bundles


def verify_bundles(root):
    """Problems with the index or any bundle's MANIFEST hashes; empty when consistent."""
    problems, seen = [], set()
    evidence = Path(root) / "evidence"
    index = load_index(root)
    for entry in index["bundles"]:
        bundle_id = entry.get("bundle_id")
        if bundle_id in seen:
            problems.append(f"{bundle_id}: duplicate bundle id in index.json")
        seen.add(bundle_id)
        if entry.get("status") not in {"current", "superseded", "withdrawn"}:
            problems.append(f"{bundle_id}: status must be current, superseded or withdrawn")
        if entry.get("status") in {"superseded", "withdrawn"} and not entry.get("reason"):
            problems.append(f"{bundle_id}: a {entry.get('status')} bundle needs a reason")
        if entry.get("provenance") not in {"recorded", "inferred"}:
            problems.append(f"{bundle_id}: provenance must be recorded or inferred")
        directory = evidence / entry.get("path", "")
        manifest_path = directory / "MANIFEST.json"
        if not manifest_path.exists():
            problems.append(f"{bundle_id}: missing MANIFEST.json")
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("bundle_id") != bundle_id or manifest.get("class") != entry.get("class"):
            problems.append(f"{bundle_id}: MANIFEST id or class differs from index.json")
        listed = set()
        for item in manifest.get("files", []):
            listed.add(item["path"])
            path = directory / item["path"]
            if not path.is_file():
                problems.append(f"{bundle_id}: missing {item['path']}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                problems.append(f"{bundle_id}: {item['path']} does not match its MANIFEST hash")
        extra = sorted(p.relative_to(directory).as_posix() for p in directory.rglob("*")
                       if p.is_file() and p.name != "MANIFEST.json")
        for relative in extra:
            if relative not in listed:
                problems.append(f"{bundle_id}: {relative} is not listed in MANIFEST.json")
    indexed = {entry.get("path") for entry in index["bundles"]}
    for manifest_path in sorted(evidence.glob("*/*/MANIFEST.json")):
        relative = manifest_path.parent.relative_to(evidence).as_posix()
        if relative not in indexed:
            problems.append(f"evidence/{relative}: bundle directory is missing from index.json")
    return problems + verify_exclusions(root, seen)


def load_exclusions(root):
    """Runs deliberately not collected, each with its reason (evidence/exclusions.json), or []."""
    path = Path(root) / EXCLUSIONS
    return json.loads(path.read_text(encoding="utf-8")).get("excluded", []) if path.exists() else []


def verify_exclusions(root, bundle_ids):
    """An excluded run needs a run name, class, revision and reason, and must not also be a bundle."""
    problems, seen = [], set()
    for entry in load_exclusions(root):
        run = str(entry.get("run") or "")
        for key in ("run", "class", "source_revision", "reason"):
            if not str(entry.get(key) or "").strip():
                problems.append(f"{EXCLUSIONS}: {run or 'an entry'} needs a {key}")
        if run in seen:
            problems.append(f"{EXCLUSIONS}: {run} is listed twice")
        seen.add(run)
        if run.lower() in bundle_ids:
            problems.append(f"{EXCLUSIONS}: {run} is both excluded and collected as a bundle")
    return problems


# ----- derivations: pure functions over bundles -----

def outcome(bundle):
    """PASS, FAILED, INTERRUPTED or INCOMPLETE for one bundle."""
    if bundle.run_class == "chaos-batch":
        status = (bundle.json("batch.json") or {}).get("status")
        return {"complete": "PASS", "failed": "FAILED"}.get(status, "INCOMPLETE")
    if bundle.run_class == "security-lab":
        validation = bundle.json("validation.json")
        if not validation or "passed" not in validation:
            return "INCOMPLETE"
        return "PASS" if validation["passed"] is True else "FAILED"
    if bundle.run_class == "guard-eval":
        results = bundle.json("results.json")
        if not results:
            return "INCOMPLETE"
        return "PASS" if results.get("expectations_met") is True else "FAILED"
    if bundle.run_class == "executor-wedge":
        return {"PASS": "PASS", "FAIL": "FAILED"}.get(wedge_summary(bundle).get("status"), "INCOMPLETE")
    if bundle.run_class == "distributed-scenario":
        summary = bundle.json("summary.json")
        if not isinstance(summary, dict) or not isinstance(summary.get("matches_expectation"), bool):
            return "INCOMPLETE"
        return "PASS" if summary["matches_expectation"] and not summary.get("harness_error") else "FAILED"
    if bundle.run_class == "executor-admission":
        return {"PASS": "PASS", "FAIL": "FAILED"}.get((bundle.json("admission.json") or {}).get("status"),
                                                       "INCOMPLETE")
    if bundle.run_class == "worker-container":
        return container_record(bundle)["outcome"]
    if bundle.run_class == "kind-lab-session":
        return lab_session_record(bundle)["outcome"]
    summary = bundle.json("summary.json") or bundle.json("data/evidence/summary.json") or {}
    status = summary.get("status")
    return status if status in OUTCOMES else "INCOMPLETE"


def summary_of(bundle):
    return bundle.json("summary.json") or bundle.json("data/evidence/summary.json") or {}


def wedge_summary(bundle):
    """The scenario summary of an executor-wedge bundle (rv-race, delete-recreate or admission-deny), or {}."""
    for name in collect.WEDGE_SUMMARIES:
        value = bundle.json(name)
        if isinstance(value, dict):
            return value
    return {}


def ratio(passed, total):
    return f"{passed}/{total}"


def pass_ratio(bundles):
    return ratio(sum(outcome(b) == "PASS" for b in bundles), len(bundles))


def failure_class(summary):
    """Short failure label from a FAILED summary; unknown text keeps its error type."""
    failure = summary.get("agent_failure")
    text = " ".join(str(part) for part in (
        failure.get("error") if isinstance(failure, dict) else failure, summary.get("error"),
        summary.get("integration_error")) if part)
    for needle, label in (("refused", "model server unreachable"), ("urlopen error", "model server unreachable"),
                          ("not found", "model not installed"), ("not installed", "model not installed"),
                          ("escalate", "agent escalated"),
                          ("not_recovered", "not recovered"), ("awaiting_approval", "stopped awaiting approval")):
        if needle in text.lower():
            code = failure.get("error_code") if isinstance(failure, dict) else None
            # The planning error code says why the agent escalated (ABSTAINED, DIAGNOSIS_OFF_TARGET, ...).
            return f"{label} ({code})" if label == "agent escalated" and code else label
    return summary.get("error_type") or summary.get("integration_error") or "unspecified"


def model_seconds(bundle):
    calls = []
    for item in bundle.manifest.get("files", []):
        if item["path"].endswith("model-calls.json"):
            calls.extend(bundle.json(item["path"]) or [])
    return round(sum(float(call.get("seconds") or 0) for call in calls), 1) if calls else None


def short(revision):
    return revision[:7] if revision and revision != "unknown" else "unknown"


def median_text(values):
    values = [v for v in values if v is not None]
    return f"{statistics.median(values):g}" if values else "n/a"


def expected_escalation(bundle):
    """True for an agent run that passed because planning escalated as asked (``--expect escalated``).

    Such a run ends in NEEDS_REVIEW with zero writes, so it is never a recovery.
    """
    return outcome(bundle) == "PASS" and summary_of(bundle).get("expected_outcome") == "escalated"


def agent_runs(bundles):
    """Rows per source revision; every run is in the denominator.

    RECOVERED counts PASS runs that expected a recovery. An expected escalation
    counts as not recovered, labelled as such.
    """
    groups = {}
    for bundle in bundles:
        if bundle.run_class == "agent":
            key = (short(bundle.entry.get("source_revision")), bundle.entry.get("provenance"))
            groups.setdefault(key, []).append(bundle)
    rows = []
    for (revision, provenance), members in sorted(groups.items(), key=lambda item: min(
            b.entry.get("captured_at") or "" for b in item[1])):
        outcomes = [outcome(b) for b in members]
        recovered = [b for b, result in zip(members, outcomes) if result == "PASS" and not expected_escalation(b)]
        failures = {}
        for bundle, result in zip(members, outcomes):
            if expected_escalation(bundle):
                failures["expected escalation"] = failures.get("expected escalation", 0) + 1
            elif result != "PASS":
                label = failure_class(summary_of(bundle)) if result == "FAILED" else result.lower()
                failures[label] = failures.get(label, 0) + 1
        calls = [summary_of(b).get("agent_model_calls") for b in recovered]
        rows.append([revision, provenance, str(len(members)), ratio(len(recovered), len(members)),
                     str(len(members) - len(recovered)), median_text(calls),
                     median_text([model_seconds(b) for b in recovered]),
                     "; ".join(f"{k} x{v}" for k, v in sorted(failures.items())) or "none"])
    return rows


def recovery_matrix(bundles, classes=("connected-harness", "shipping-lab", "packaged")):
    """Rows of (class, check name, passed/runs); runs include failed and incomplete bundles."""
    rows = []
    for run_class in classes:
        members = [b for b in bundles if b.run_class == run_class]
        names = []
        for bundle in members:
            for check in summary_of(bundle).get("checks", []):
                if check.get("name") not in names:
                    names.append(check.get("name"))
        for name in names:
            passed = sum(any(c.get("name") == name and c.get("passed") is True for c in summary_of(b).get("checks", []))
                         for b in members)
            rows.append([run_class, name, ratio(passed, len(members))])
    return rows


def guard_rejections(bundles):
    """Rejection runs, refused, and the Executor writes counted for the refused action."""
    members = [b for b in bundles if b.run_class == "guard-rejection"]
    refused, writes, unknown = 0, 0, 0
    for bundle in members:
        record = summary_of(bundle).get("guard_rejection") or {}
        attempts = record.get("executor_patch_attempts")
        if outcome(bundle) == "PASS" and record.get("guard_verdict") == "REJECT":
            refused += 1
        if isinstance(attempts, int):
            writes += attempts
        else:
            unknown += 1
    return {"runs": len(members), "refused": refused, "writes": writes, "writes_unknown": unknown}


def guard_evaluation(bundles):
    """One row per configuration of each offline Guard fixture evaluation bundle."""
    rows = []
    members = sorted((b for b in bundles if b.run_class == "guard-eval"),
                     key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id))
    for bundle in members:
        results = bundle.json("results.json") or {}
        for name, configuration in (results.get("configurations") or {}).items():
            summary = configuration.get("summary") or {}
            counts = summary.get("verdict_counts") or {}
            safe, unsafe = counts.get("SAFE") or {}, counts.get("UNSAFE") or {}
            n_safe, n_unsafe = summary.get("safe_cases", 0), summary.get("unsafe_cases", 0)
            rows.append([bundle.bundle_id, results.get("status", "unknown"), name, configuration.get("basis", "measured"),
                         ratio(unsafe.get("ALLOW", 0), n_unsafe), ratio(unsafe.get("REJECT", 0), n_unsafe),
                         ratio(unsafe.get("ESCALATE", 0), n_unsafe), ratio(safe.get("ALLOW", 0), n_safe),
                         ratio(safe.get("REJECT", 0), n_safe), str(summary.get("errors", 0))])
    return rows


def guard_unsafe_acceptance(bundles):
    """Unsafe ALLOW per configuration in the newest cited Guard evaluation."""
    rows = guard_evaluation(bundles)
    if not rows:
        return "no Guard evaluation"
    newest = rows[-1][0]
    return ", ".join(f"{row[2]} {row[4]}" + (" (definitional)" if row[3] == "definitional" else "")
                     for row in rows if row[0] == newest)


def zero_write_rejections(bundles):
    result = guard_rejections(bundles)
    suffix = f", {result['writes_unknown']} unmeasured" if result["writes_unknown"] else ""
    return f"{ratio(result['refused'], result['runs'])} refused, {result['writes']} Executor writes{suffix}"


def _seconds(start, end):
    if not start or not end:
        return None
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    return round((parse(end) - parse(start)).total_seconds(), 1)


def _phase(timeline, name, scenario):
    return next((step["at"] for step in timeline if step.get("phase") == name and step.get("scenario") == scenario),
                None)


def timings(bundles, classes=("agent", "connected-harness")):
    """Interval rows; a timeline gives 'recorded' values, file timestamps give 'approx'."""
    rows = []
    for run_class in classes:
        intervals = {"fault -> incident observed": [], "incident observed -> repair applied": [],
                     "repair applied -> outcome window end": []}
        sources = {name: set() for name in intervals}
        for bundle in bundles:
            if bundle.run_class != run_class or outcome(bundle) != "PASS":
                continue
            timeline = summary_of(bundle).get("timeline") or []
            incident = bundle.json("primary-incident.json") or {}
            execution = bundle.json("approved-rollback/execution.json") or {}
            result = bundle.json("approved-rollback/outcome.json") or {}
            recorded_fault = _phase(timeline, "fault_injected", "primary")
            recorded_incident = _phase(timeline, "incident_observed", "primary")
            recorded_execution = _phase(timeline, "execution_recorded", "approved-rollback")
            values = (
                ("fault -> incident observed", _seconds(recorded_fault, recorded_incident), "recorded"),
                ("incident observed -> repair applied",
                 *((_seconds(recorded_incident, recorded_execution), "recorded")
                   if recorded_incident and recorded_execution else
                   (_seconds(incident.get("observed_at"), execution.get("recorded_at")), "approx"))),
                ("repair applied -> outcome window end",
                 *((_seconds(recorded_execution, result.get("window_end")), "recorded") if recorded_execution else
                   (_seconds(execution.get("recorded_at"), result.get("window_end")), "approx"))),
            )
            for name, value, source in values:
                if value is not None:
                    intervals[name].append(value)
                    sources[name].add(source)
        for name, values in intervals.items():
            if values:
                source = "recorded" if sources[name] == {"recorded"} else "approx"
                rows.append([run_class, name, str(len(values)), f"{statistics.median(values):g}",
                             f"{min(values):g}", f"{max(values):g}", source])
    return rows


def chaos(bundles):
    """Per fault: planned attempts, confirmed injection, cleanup and healthy recovery window."""
    faults = {}
    for bundle in bundles:
        if bundle.run_class != "chaos-batch":
            continue
        batch = bundle.json("batch.json") or {}
        runs = list(batch.get("runs") or [])
        for fault in (batch.get("configuration") or {}).get("faults") or []:
            row = faults.setdefault(fault, [0, 0, 0, 0])
            row[0] += 1
            match = next((r for r in runs if (r.get("run_id") or "").split("-")[1:2] == [fault]), None)
            if match:
                runs.remove(match)
                row[1] += match.get("injection_confirmed") is True
                row[2] += match.get("cleanup_confirmed") is True
                row[3] += match.get("recovery_healthy") is True
    return [[fault, str(a), ratio(i, a), ratio(c, a), ratio(r, a)] for fault, (a, i, c, r) in sorted(faults.items())]


def chaos_injection(bundles):
    rows = chaos(bundles)
    attempts = sum(int(row[1]) for row in rows)
    injected = sum(int(row[2].split("/")[0]) for row in rows)
    cleaned = sum(int(row[3].split("/")[0]) for row in rows)
    return f"injected {ratio(injected, attempts)}, cleaned up {ratio(cleaned, attempts)}"


def security_lab(bundles):
    rows = []
    for bundle in bundles:
        if bundle.run_class != "security-lab":
            continue
        validation = bundle.json("validation.json") or {}
        blocked = sum(isinstance(validation.get(probe), dict) and validation[probe].get("succeeded") is False
                      for probe in SECURITY_PROBES)
        quote = validation.get("valid_mtls_quote") or {}
        restore = validation.get("shopping_after_proxy_restore") or {}
        rows.append([bundle.bundle_id, outcome(bundle), "yes" if quote.get("succeeded") is True else "no",
                     ratio(blocked, len(SECURITY_PROBES)), "yes" if restore.get("passed") is True else "no"])
    return rows


def observer(bundles):
    rows = []
    for bundle in bundles:
        if bundle.run_class != "observer-acceptance":
            continue
        summary = summary_of(bundle)
        value = lambda key: f"{summary[key]:.1f}" if isinstance(summary.get(key), (int, float)) else "n/a"
        rows.append([bundle.bundle_id, outcome(bundle), value("detection_elapsed_seconds"),
                     value("duplicate_suppression_seconds")])
    return rows


# ----- Phase 3: distributed scenarios, admission policy, worker containers, kind lab sessions -----

def distributed_summary(bundle):
    value = bundle.json("summary.json") if bundle.run_class == "distributed-scenario" else None
    return value if isinstance(value, dict) else {}


def failed_invariants(summary):
    return [item.get("id") for item in summary.get("invariants") or []
            if isinstance(item, dict) and item.get("passed") is not True]


def fault_visible_after(bundle):
    """Seconds from the kind R1 injection until the target showed it: the run's kind_lab block, else the wrapper."""
    fault = ((distributed_summary(bundle).get("kind_lab") or {}).get("fault") or {})
    if isinstance(fault.get("visible_after_seconds"), (int, float)):
        return fault["visible_after_seconds"]
    wrapper = bundle.json("kind-wrapper.json") or {}
    injected, visible = (wrapper.get("inject") or {}).get("at"), (wrapper.get("fault_visible") or {}).get("at")
    return _seconds(injected, visible) if injected and visible else None


def distributed_rows(bundles):
    members = sorted((b for b in bundles if b.run_class == "distributed-scenario"),
                     key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id))
    rows = []
    for bundle in members:
        summary = distributed_summary(bundle)
        failed = failed_invariants(summary)
        visible = fault_visible_after(bundle)
        rows.append([bundle.bundle_id, summary.get("mode") or "unknown", summary.get("scenario") or "unknown",
                     short(bundle.entry.get("source_revision")), outcome(bundle),
                     ", ".join(summary.get("states") or []) or "none", str(summary.get("committed_writes", "n/a")),
                     str(summary.get("unobserved_windows", "n/a")), ", ".join(failed) or "none",
                     str(summary.get("duration_seconds", "n/a")), str(summary.get("deadline_seconds", "n/a")),
                     "n/a" if visible is None else f"{visible:g}"])
    return rows


def distributed_expectations(bundles):
    """Cited scenario runs that matched their expectation, per scenario, with every failed invariant named."""
    summaries = [(bundle, distributed_summary(bundle)) for bundle in bundles]
    per = {}
    for bundle, summary in summaries:
        name = summary.get("scenario") or "unknown"
        row = per.setdefault(name, [0, 0])
        row[0] += outcome(bundle) == "PASS"
        row[1] += 1
    held = sum(bool(summary.get("invariants")) and not failed_invariants(summary) for _, summary in summaries)
    failures = [f"{summary.get('scenario') or 'unknown'} {', '.join(failed_invariants(summary))}"
                for _, summary in summaries if failed_invariants(summary)]
    windows = sum(int(summary.get("unobserved_windows") or 0) for _, summary in summaries)
    errors = sum(bool(summary.get("harness_error")) for _, summary in summaries)
    text = (f"{ratio(sum(outcome(b) == 'PASS' for b in bundles), len(bundles))} matched their expectation ("
            + ", ".join(f"{name} {ratio(a, n)}" for name, (a, n) in per.items())
            + f"); every invariant held in {ratio(held, len(bundles))}")
    if failures:
        text += "; failed: " + "; ".join(failures)
    return text + f"; {windows} unobserved windows; {errors} harness errors"


def _sealed(bundle):
    for record in bundle.jsonl("timeline.jsonl") or []:
        if record.get("event") == "executor.seal.sealed":
            return record
    return {}


def _conflicts(bundle, identity):
    """'operation reason' of each HTTP 409 the Coordinator answered to one identity, in log order."""
    return [f"{record.get('operation')} {record.get('reason')}" for record in bundle.jsonl("timeline.jsonl") or []
            if record.get("event") == "http.access" and record.get("http_status") == 409
            and record.get("identity") == identity]


def seal_outcomes(bundles):
    """Per cited S8 run: who paused and who sealed, at which epoch, and what the paused write and Executor got."""
    parts = []
    for bundle in bundles:
        summary = distributed_summary(bundle)
        notes, sealed = summary.get("notes") or {}, _sealed(bundle)
        paused = notes.get("paused") or "unknown"
        conflicts = _conflicts(bundle, paused)
        parts.append(f"{short(bundle.entry.get('source_revision'))} {outcome(bundle)}: {paused} paused, "
                     f"{sealed.get('identity') or 'no Executor'} sealed at fence epoch "
                     f"{sealed.get('fence_epoch', 'n/a')} ({sealed.get('basis') or 'no seal record'}), the paused "
                     f"patch {notes.get('paused_result') or 'unknown'}, {notes.get('writes_after_release', 'n/a')} "
                     f"writes after release, {summary.get('committed_writes', 'n/a')} committed template writes, "
                     f"incident {', '.join(summary.get('states') or []) or 'unknown'}, HTTP 409 to {paused}: "
                     f"{', '.join(conflicts) or 'none'}")
    return "; ".join(parts) or "no S8 runs"


def trace_shapes(bundles):
    """Per cited run: traces in its timeline, spans, wall time and spans whose parent span never ended."""
    parts = []
    for bundle in bundles:
        summary = distributed_summary(bundle)
        records = trace_view.read_lines((bundle.text("timeline.jsonl") or "").splitlines())
        traces = {key: value for key, value in trace_view.build(records).items() if value["spans"]}
        spans = [span for trace in traces.values() for span in trace["spans"].values()]
        orphans = sum(bool(root.get("missing_parent")) for trace in traces.values() for root in trace["roots"])
        first = [root.get("span_name") for trace in traces.values() for root in trace["roots"]
                 if not root.get("missing_parent")]
        wall = max(s["end"] for s in spans) - min(s["start"] for s in spans) if spans else 0
        parts.append(f"{summary.get('scenario') or 'unknown'} {outcome(bundle)}: {len(traces)} trace"
                     f"{'' if len(traces) == 1 else 's'}, {len(spans)} spans over {wall:.1f} s from "
                     f"{', '.join(first) or 'no root'}, {orphans} span{'' if orphans == 1 else 's'} whose parent "
                     "never ended")
    return "; ".join(parts) or "no traced runs"


def admission_record(bundle):
    record = bundle.json("admission.json") or {}
    probes = [check for check in record.get("checks") or [] if isinstance(check, dict) and check.get("expected")]
    expected = lambda name: [check for check in probes if check.get("expected") == name]
    policy = expected("DENIED_BY_POLICY")
    return {"probes": probes, "passed": sum(check.get("passed") is True for check in probes),
            "allowed": len(expected("ALLOWED")), "policy": len(policy),
            "policy_named_unchanged": sum(check.get("rule_named") is True and check.get("object_unchanged") is True
                                          for check in policy),
            "rbac": len(expected("DENIED_BY_RBAC")),
            "cleaned": all(record.get(key) is True for key in ("namespace_deleted", "policy_deleted",
                                                                "private_credentials_removed")),
            "server": record.get("server_version")}


def admission_rows(bundles):
    rows = []
    for bundle in sorted((b for b in bundles if b.run_class == "executor-admission"),
                         key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id)):
        record = admission_record(bundle)
        rows.append([bundle.bundle_id, short(bundle.entry.get("source_revision")), bundle.entry.get("provenance"),
                     outcome(bundle), ratio(record["passed"], len(record["probes"])), str(record["allowed"]),
                     f"{record['policy']} ({record['policy_named_unchanged']} with the rule named and the object "
                     "unchanged)", str(record["rbac"]), "yes" if record["cleaned"] else "no"])
    return rows


def admission_probes(bundles):
    parts = []
    for bundle in bundles:
        record = admission_record(bundle)
        named = record["policy_named_unchanged"]
        named = f"all {named}" if named == record["policy"] else f"{named} of them"
        parts.append(f"{ratio(record['passed'], len(record['probes']))} probes as expected on Kubernetes "
                     f"{record['server'] or 'unknown'} ({record['allowed']} allowed, {record['policy']} denied by the "
                     f"policy, {named} with the rule named and the object "
                     f"unchanged, {record['rbac']} denied by RBAC); namespace, policy and private credentials "
                     f"{'removed' if record['cleaned'] else 'NOT all removed'}")
    return "; ".join(parts) or "no admission runs"


def _contact_age(sample):
    text = str(sample.get("readyz") or "")
    try:
        return json.loads(text.split(" ", 1)[1]).get("contact_age_seconds")
    except (IndexError, ValueError, AttributeError):
        return None


def _planning_passes(bundle):
    """Durations (s) of the Planner's worker.process spans for planner.request in the container logs."""
    found = []
    for item in bundle.manifest.get("files", []):
        if not item["path"].endswith(".log") or "planner" not in item["path"]:
            continue
        for line in (bundle.text(item["path"]) or "").splitlines():
            start = line.find("{")
            try:
                record = json.loads(line[start:]) if start >= 0 else None
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("event") == "span.end" \
                    and record.get("span_name") == "worker.process" and record.get("topic") == "planner.request" \
                    and isinstance(record.get("duration_ms"), (int, float)):
                found.append(record["duration_ms"] / 1000)
    return found


def container_record(bundle):
    """Readiness of a worker container while its Coordinator was up, and how soon it turned unready after the stop.

    Needs the orchestrator's events.jsonl and health.jsonl; without them the outcome is INCOMPLETE.
    """
    events, health = bundle.jsonl("events.jsonl"), bundle.jsonl("health.jsonl")
    if not events or not health:
        passes = _planning_passes(bundle)
        return {"outcome": "INCOMPLETE", "longest_pass": max(passes) if passes else None}
    by_event = {event.get("event"): event for event in events}
    stopped = (by_event.get("coordinator_stopped") or {}).get("utc")
    first = next((index for index, sample in enumerate(health) if sample.get("health") == "healthy"), None)
    window = [sample for sample in health[first:] if stopped and str(sample.get("utc")) < stopped] \
        if first is not None else []
    good = [sample for sample in window if sample.get("health") == "healthy"
            and str(sample.get("readyz") or "").startswith("200")]
    # Every sample taken while the Coordinator was up, including any before Docker's first healthy check.
    up = [sample for sample in health if stopped and str(sample.get("utc")) < stopped]
    ready_up = [sample for sample in up if str(sample.get("readyz") or "").startswith("200")]
    ages = [age for age in (_contact_age(sample) for sample in window) if isinstance(age, (int, float))]
    passes = _planning_passes(bundle)
    after_503 = (by_event.get("readyz_503") or {}).get("after_stop_seconds")
    after_unhealthy = (by_event.get("docker_unhealthy") or {}).get("after_stop_seconds")
    passed = ("result" in by_event and bool(window) and len(good) == len(window) and bool(passes)
              and after_503 is not None and after_unhealthy is not None)
    return {"outcome": "PASS" if passed else "FAILED", "window": len(window), "good": len(good),
            "up": len(up), "ready_up": len(ready_up),
            "max_age": max(ages) if ages else None, "longest_pass": max(passes) if passes else None,
            "after_503": after_503, "after_unhealthy": after_unhealthy}


def docker_states(bundle):
    """Per collected `docker inspect` record (*.state.json): the container's Docker health status when it was saved
    and how many of the health checks Docker still kept (its last five) had failed."""
    parts = []
    for item in bundle.manifest.get("files", []):
        if not item["path"].endswith(".state.json"):
            continue
        value = bundle.json(item["path"])
        value = value[0] if isinstance(value, list) and value else value
        state = (value.get("State") or value) if isinstance(value, dict) else {}
        health = state.get("Health") if isinstance(state.get("Health"), dict) else {}
        checks = [entry for entry in health.get("Log") or [] if isinstance(entry, dict)]
        failed = sum(entry.get("ExitCode") != 0 for entry in checks)
        name = item["path"].rsplit("/", 1)[-1][:-len(".state.json")]
        parts.append(f"{name}: {health.get('Status') or 'no health status'}, "
                     f"{ratio(failed, len(checks))} kept checks failed")
    return "; ".join(parts) or "not recorded"


def container_health(bundles):
    """Docker's saved health record of every container in the cited bundles, in the cited order."""
    return "; ".join(docker_states(bundle) for bundle in bundles) or "no container runs"


def container_rows(bundles):
    rows = []
    for bundle in sorted((b for b in bundles if b.run_class == "worker-container"),
                         key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id)):
        record = container_record(bundle)
        value = lambda key: "n/a" if record.get(key) is None else f"{record[key]:.1f}"
        rows.append([bundle.bundle_id, short(bundle.entry.get("source_revision")), record["outcome"],
                     ratio(record["good"], record["window"]) if "window" in record else "not recorded",
                     ratio(record["ready_up"], record["up"]) if "up" in record else "not recorded",
                     value("longest_pass"), value("max_age"), value("after_503"), value("after_unhealthy"),
                     docker_states(bundle)])
    return rows


def container_readiness(bundles):
    parts = []
    for bundle in bundles:
        record = container_record(bundle)
        if "window" not in record:
            parts.append(f"{bundle.bundle_id}: no orchestrator health record")
            continue
        fmt = lambda key: "n/a" if record.get(key) is None else f"{record[key]:.1f} s"
        parts.append(f"{ratio(record['good'], record['window'])} health samples Docker healthy with /readyz 200 "
                     f"from the first Docker-healthy one until the Coordinator stopped ({record['up']} taken while "
                     f"it was up, {ratio(record['ready_up'], record['up'])} answering /readyz 200; longest planning "
                     f"pass {fmt('longest_pass')}, highest contact age {fmt('max_age')}); /readyz 503 "
                     f"{fmt('after_503')} and Docker unhealthy {fmt('after_unhealthy')} after the Coordinator stopped")
    return "; ".join(parts) or "no container runs"


def lab_session_record(bundle):
    try:
        prepare = json.loads(bundle.text("prepare.out") or "null")
    except ValueError:
        prepare = None
    rbac = prepare.get("rbac") if isinstance(prepare, dict) else None
    rbac = rbac if isinstance(rbac, list) else bundle.json("bundle/rbac-checks.json")
    if not isinstance(rbac, list) or not rbac:
        return {"outcome": "INCOMPLETE"}
    matched = sum(isinstance(check, dict) and check.get("answer") == check.get("expected") for check in rbac)
    restore = bundle.json("restore-check.json")
    restored = None if restore is None else (restore.get("healthy") is True
                                             and (restore.get("after") or {}).get("template_is_baseline") is True)
    outcome_ = "PASS" if matched == len(rbac) and restored is not False else "FAILED"
    return {"outcome": outcome_, "matched": matched, "checks": len(rbac), "restored": restored,
            "restore_seconds": (restore or {}).get("seconds")}


def lab_session_rows(bundles):
    rows = []
    for bundle in sorted((b for b in bundles if b.run_class == "kind-lab-session"),
                         key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id)):
        record = lab_session_record(bundle)
        restore = {None: "not run", True: f"healthy at baseline after {record.get('restore_seconds')} s",
                   False: "not healthy"}[record.get("restored")]
        rows.append([bundle.bundle_id, short(bundle.entry.get("source_revision")), record["outcome"],
                     ratio(record.get("matched", 0), record.get("checks", 0)), restore])
    return rows


def lab_sessions(bundles):
    parts = []
    for bundle in bundles:
        record = lab_session_record(bundle)
        restore = {None: "no restore check", True: f"the restore check returned the target healthy at its baseline "
                   f"template in {record.get('restore_seconds')} s", False: "the restore check did NOT end healthy"}
        parts.append(f"{short(bundle.entry.get('source_revision'))}: Executor RBAC checks "
                     f"{ratio(record.get('matched', 0), record.get('checks', 0))} as expected, "
                     + restore[record.get("restored")])
    return "; ".join(parts) or "no kind lab sessions"


# ----- latency ledger (scripts/latency_report.py) -----

LATENCY_DIR = "evidence/evaluation/latency"
LATENCY_CLASSES = {"agent": "agent", "connected-harness": "harness"}
NO_LEDGER = "_No latency ledger yet._"
STAGE_LABELS = {
    "detection": "detection: start -> incident observed",
    "planning": "planning: incident -> last planning record",
    "model": "model calls: sum per run (part of planning)",
    "guard": "Guard: planning end -> ALLOW (incl. lab approval)",
    "decision": "incident -> Guard ALLOW (planning + Guard)",
    "execution": "execution: ALLOW -> Executor result",
    "verification": "verification: Executor result -> outcome window end",
    "repair": "fault to repair: start -> Executor result",
    "total": "total: start -> outcome window end",
}
BASIS_LABELS = {"fault_injected_at": "injection", "timeline": "injection (timeline)",
                "from_first_failing_snapshot": "first failing snapshot"}


def load_latency(root):
    """(relative path, ledger) of the newest committed latency ledger, or (None, None)."""
    folder = Path(root) / LATENCY_DIR
    paths = sorted(folder.glob("*/timings.json")) if folder.is_dir() else []
    if not paths:
        return None, None
    return paths[-1].relative_to(root).as_posix(), json.loads(paths[-1].read_text(encoding="utf-8"))


def latency_rows(ledger, bundles):
    """Ledger rows for current or superseded bundles; withdrawn bundles drop out."""
    present = {bundle.bundle_id for bundle in bundles}
    return [row for row in (ledger or {}).get("rows", []) if row.get("bundle_id") in present]


def check_latency(root, bundles=None):
    """Problems with the latency ledger: a bundle-sourced value no longer matches its bundle, or rows are missing."""
    bundles = load_bundles(root) if bundles is None else bundles
    relative, ledger = load_latency(root)
    if ledger is None:
        return []
    problems = []
    rows = {row.get("bundle_id"): row for row in ledger.get("rows", [])}
    indexed = {entry["bundle_id"] for entry in load_index(root)["bundles"]}
    for bundle_id in sorted(set(rows) - indexed, key=str):
        problems.append(f"{relative}: row {bundle_id} is not a bundle in index.json")
    for bundle in bundles:
        if bundle.run_class not in LATENCY_CLASSES:
            continue
        row = rows.get(bundle.bundle_id)
        if row is None:
            problems.append(f"{relative}: bundle {bundle.bundle_id} is missing; run scripts/latency_report.py "
                            "into a new dated folder")
            continue
        fresh = timeline.run_timeline([(bundle.directory, timeline.BUNDLE)], run=row.get("run"),
                                      bundle_id=bundle.bundle_id, planning_mode=LATENCY_CLASSES[bundle.run_class])
        for name in timeline.STAGES:
            source = (row.get("stage_sources") or {}).get(name)
            recorded = (row.get("stages") or {}).get(name)
            if source not in (None, timeline.BUNDLE, timeline.RUN_FOLDER) or (
                    source == timeline.BUNDLE and recorded != fresh["stages"][name]) or (
                    source is None and (recorded is not None or fresh["stages"][name] is not None)):
                problems.append(f"{relative}: {bundle.bundle_id} {name} {recorded!r} ({source}) differs from "
                                f"the bundle ({fresh['stages'][name]!r})")
        for key in ("planning_mode", "status", "outcome", "failure_stage", "start_basis", "calls"):
            if row.get(key) != fresh[key]:
                problems.append(f"{relative}: {bundle.bundle_id} {key} differs from the bundle")
    return problems


def seconds_text(value):
    return "n/a" if value is None else f"{value:.1f}"


def latency_tables(ledger, bundles):
    """(stage table, model call table) markdown; stages with no data show n/a, never zero."""
    rows = latency_rows(ledger, bundles)
    if not rows:
        return NO_LEDGER, NO_LEDGER
    notes, stage_rows, model_rows = [], [], []
    for mode, summary in timeline.aggregate(rows).items():
        members = [row for row in rows if row["planning_mode"] == mode]
        stops = "; ".join(f"{stage} x{count}" for stage, count in summary["failure_stages"].items()) or "none"
        notes.append(f"- {mode}: {summary['runs']} runs, {summary['passed']} PASS; stopped at: {stops}.")
        for name in timeline.STAGES:
            values = summary["stages"][name]
            kinds = {row["stage_sources"][name] for row in members if row["stages"][name] is not None}
            source = {frozenset({timeline.BUNDLE}): "bundle", frozenset({timeline.RUN_FOLDER}): "run folder"}.get(
                frozenset(kinds), "bundle + run folder" if kinds else "-")
            if name in ("detection", "repair", "total") and kinds:
                bases = {}
                for row in members:
                    if row["stages"][name] is not None:
                        label = BASIS_LABELS.get(row["start_basis"], str(row["start_basis"]))
                        bases[label] = bases.get(label, 0) + 1
                source += "; start: " + ", ".join(f"{label} x{count}" for label, count in sorted(bases.items()))
            stage_rows.append([mode, STAGE_LABELS[name], str(values["n"]), seconds_text(values["median"]),
                               seconds_text(values["p90"]), seconds_text(values["min"]), seconds_text(values["max"]),
                               source])
        if any(row["calls"] for row in members):
            for name in ("warm", "cold", "unclassified"):
                values = summary["model_by_temperature"][name]
                model_rows.append([mode, f"model time per run, {name} runs", str(values["n"]),
                                   seconds_text(values["median"]), seconds_text(values["p90"]),
                                   seconds_text(values["max"])])
            for name in ("warm", "cold", "unclassified"):
                values = summary["calls_by_temperature"][name]
                model_rows.append([mode, f"one call, {name}", str(values["n"]), seconds_text(values["median"]),
                                   seconds_text(values["p90"]), seconds_text(values["max"])])
            values = summary["cold_load"]
            model_rows.append([mode, "model load time of cold calls", str(values["n"]), seconds_text(values["median"]),
                               seconds_text(values["p90"]), seconds_text(values["max"])])
    stages = "\n".join(notes) + "\n\n" + table(
        ["Mode", "Stage", "n", "Median s", "p90 s", "Min s", "Max s", "Source"], stage_rows)
    model = table(["Mode", "Measure", "n", "Median s", "p90 s", "Max s"], model_rows)
    return stages, model


FAIL_CLOSED = ("model server unreachable", "model not installed")
# Files a run writes once a proposal reaches Guard or the Executor records an attempt.
DOWNSTREAM_RECORDS = ("proposal.json", "initial-guard.json", "execution.json")


def _has_file(bundle, name):
    return any(item["path"] == name or item["path"].endswith("/" + name) for item in bundle.manifest.get("files", []))


def fail_closed(bundles):
    """Cited runs that failed on an unreachable or missing model and left no Guard or Executor record."""
    stopped = sum(outcome(b) == "FAILED" and failure_class(summary_of(b)) in FAIL_CLOSED
                  and not any(_has_file(b, name) for name in DOWNSTREAM_RECORDS) for b in bundles)
    return ratio(stopped, len(bundles))


def preflight_stops(bundles):
    """Cited runs that failed at the model preflight, before any fault was injected, with no Guard or Executor record."""
    stopped = 0
    for bundle in bundles:
        failure = summary_of(bundle).get("agent_failure")
        stopped += (outcome(bundle) == "FAILED" and isinstance(failure, dict) and failure.get("stage") == "preflight"
                    and failure.get("fault_injected") is False
                    and not any(_has_file(bundle, name) for name in DOWNSTREAM_RECORDS))
    return ratio(stopped, len(bundles))


def wedge_row(bundle):
    """Scenario, revision, outcome, reconcile result, reconcile passes and second proposal for one wedge bundle."""
    summary = wedge_summary(bundle)
    results = {item.get("label"): item for item in summary.get("results") or [] if isinstance(item, dict)}
    reconcile = results.get("reconcile") or {}
    status = reconcile.get("status") or "none"
    resolved = status + ("/" + reconcile["basis"] if reconcile.get("basis") else "")
    passes = list(summary.get("reconcile_pass_statuses") or [])
    second = results.get("second execute")
    if second is not None:
        admitted = second.get("status") or "none"
    elif any(check.get("name") == "a new proposal for the workload is admitted" and check.get("passed") is False
             for check in summary.get("checks") or []):
        admitted = "not admitted"
    else:
        admitted = "not run"
    return [bundle.bundle_id, summary.get("scenario") or "unknown", short(bundle.entry.get("source_revision")),
            bundle.entry.get("provenance"), outcome(bundle), resolved,
            f"{passes.count(status)}/{len(passes)} {status}" if passes else "none", admitted]


def wedge_rows(bundles):
    members = sorted((b for b in bundles if b.run_class == "executor-wedge"),
                     key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id))
    return [wedge_row(bundle) for bundle in members]


def wedge_resolutions(bundles):
    """Per cited wedge run, in claim order: how the first action resolved and whether a second proposal ran."""
    parts = []
    for bundle in bundles:
        _, scenario, revision, _, result, resolved, passes, admitted = wedge_row(bundle)
        count = passes.split(" ")[0]
        parts.append(f"{scenario} at {revision} {result}: {resolved} on {count} reconcile passes, "
                     f"second proposal {admitted}")
    return "; ".join(parts) or "no wedge runs"


def recovered_ratio(bundles):
    """Cited runs that passed expecting a recovery; an expected escalation passes but never recovers."""
    return ratio(sum(outcome(b) == "PASS" and not expected_escalation(b) for b in bundles), len(bundles))


def expected_escalations(bundles):
    """Cited runs that passed as an expected escalation and left no Executor record."""
    return ratio(sum(expected_escalation(b) and not _has_file(b, "execution.json") for b in bundles), len(bundles))


def approval_refusals(bundles):
    """Cited runs stopped by a refused approval, with no approval granted and no execution for the refused action."""
    stopped = 0
    for bundle in bundles:
        approval = summary_of(bundle).get("operator_approval") or {}
        stopped += (outcome(bundle) == "FAILED" and approval.get("mode") == "reject"
                    and bool(approval.get("refusals")) and not approval.get("approvals")
                    and not _has_file(bundle, "approved-rollback/execution.json"))
    return ratio(stopped, len(bundles))


def log_tool_results(bundle):
    """Results of the agent's logs tool in the bundle's collected branch results."""
    found = []
    for item in bundle.manifest.get("files", []):
        if "/branch-" in item["path"] and item["path"].endswith("/result.json"):
            for output in (bundle.json(item["path"]) or {}).get("outputs") or []:
                if isinstance(output, dict) and output.get("tool") == "logs":
                    found.append(output.get("result") or {})
    return found


def injection_free(bundles):
    """Cited runs with no injection finding and no flagged log line in any collected logs-tool result.

    A run with no collected logs-tool result does not count: absence of a finding
    proves nothing unless the gate's inputs were seen.
    """
    clean = 0
    for bundle in bundles:
        results = log_tool_results(bundle)
        clean += (bool(results) and "injection_finding" not in summary_of(bundle)
                  and all(result.get("suspected_injection_lines") == 0 for result in results))
    return ratio(clean, len(bundles))


def scenario_result(bundle):
    return bundle.json("scenario-result.json") or summary_of(bundle).get("scenario_result") or {}


def forced_text(item):
    """One forced wrong action: its verdict and reason codes (or the Executor status when no verdict came back)."""
    verdict = item.get("guard_verdict")
    codes = ", ".join(item.get("guard_reason_codes") or [])
    said = f"{verdict} {codes}".strip() if verdict else f"no Guard verdict, Executor {item.get('executor_status')}"
    writes = item.get("executor_patch_attempts")
    return (f"{item.get('action') or 'unknown'} {said}, "
            f"{writes if isinstance(writes, int) else 'unmeasured'} write{'' if writes == 1 else 's'}")


def chaos_recovery_rows(bundles):
    """One row per catalog run with a Chaos Mesh fault: decision, writes, RESTART attribution, forced wrong actions.

    Agent runs of a chaos scenario are listed too. Attribution reads "n/a"
    when the run made no RESTART to attribute.
    """
    members = sorted((b for b in bundles if b.run_class in SCENARIO_CLASSES and summary_of(b).get("chaos")),
                     key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id))
    rows = []
    for bundle in members:
        summary, result = summary_of(bundle), scenario_result(bundle)
        attribution = summary.get("attribution")
        attributed = "n/a" if not isinstance(attribution, dict) else (
            "yes" if attribution.get("attributed") is True else "no")
        forced = "; ".join(forced_text(item) for item in summary.get("forced_refusals") or []) or "none"
        writes = result.get("executor_writes")
        cleanup = summary.get("chaos_cleanup_confirmed")
        rows.append([bundle.bundle_id, result.get("scenario") or "unknown", summary["chaos"].get("kind") or "unknown",
                     short(bundle.entry.get("source_revision")), outcome(bundle), result.get("actual") or "no decision",
                     str(writes) if isinstance(writes, int) else "unmeasured", attributed, forced,
                     {True: "yes", False: "no"}.get(cleanup, "unknown")])
    return rows


def unrefused_cells(bundle):
    """Forced wrong actions the run expected the Guard to refuse with a named reason that it did not refuse with."""
    missed = []
    for item in summary_of(bundle).get("forced_refusals") or []:
        expected = item.get("expected_reason")
        if not expected or expected in (item.get("guard_reason_codes") or []):
            continue
        cell = ("drift cell" if str(item.get("label") or "").endswith("-drift")
                else f"forced {item.get('action') or 'unknown'} cell")
        verdict = item.get("guard_verdict")
        missed.append(f"{cell} not refused ({f'Guard {verdict}' if verdict else 'no Guard verdict'}, "
                      f"Executor {item.get('executor_status') or 'status unknown'})")
    return missed


def match_text(bundle, result):
    """Whether the decision matched its pre-registered outcome, never reading as a pass for a run that failed.

    A run can decide the pre-registered action and still fail a later check
    (a forced wrong action that was not refused, say); the failure is named.
    """
    if result.get("matched_expected") is not True:
        return "no"
    status = outcome(bundle)
    if status == "PASS":
        return "yes"
    why = unrefused_cells(bundle) or [failure_class(summary_of(bundle))]
    return f"action matched; run {status}: {' and '.join(why)}"


def scenario_parts(bundle):
    """Scenario, revision, outcome, decision, how it was decided, writes, state, match and labels for one run."""
    result = scenario_result(bundle)
    source, codes = result.get("decision_source"), list(result.get("eligibility_codes") or [])
    if source == "planning_failure":
        how = f"from a planning failure ({result.get('planning_error_code') or 'no code'})"
    elif source in ("rule", "model"):
        how = f"decided by the {source}" + (f" ({', '.join(codes)})" if codes else "")
    else:
        how = f"from {source or 'an unrecorded source'}"
    writes = result.get("executor_writes")
    labels = (result.get("labels") or {}).get("status") or ("pending" if result.get("labels_pending") else "unknown")
    return [result.get("scenario") or "unknown", short(bundle.entry.get("source_revision")), outcome(bundle),
            result.get("actual") or "no decision", how, str(writes) if isinstance(writes, int) else "unmeasured",
            result.get("coordinator_state") or ("no incident" if result.get("actual") == "NO_INCIDENT" else "unknown"),
            match_text(bundle, result), labels]


def scenario_rows(bundles):
    members = sorted((b for b in bundles if b.run_class in SCENARIO_CLASSES and scenario_result(b)),
                     key=lambda b: (b.entry.get("captured_at") or "", b.bundle_id))
    return [[bundle.bundle_id, *scenario_parts(bundle)] for bundle in members]


def scenario_outcomes(bundles):
    """Per cited catalog run, in claim order, with consecutive identical runs counted once."""
    groups = []
    for bundle in bundles:
        name, revision, result, actual, how, writes, state, matched, labels = scenario_parts(bundle)
        head = f"{name} at {revision} {result}"
        if matched == "yes":
            said = "matched the pre-registered expected outcome"
        elif matched == "no":
            said = "did not match the pre-registered expected outcome"
        else:
            said = ("the action matched the pre-registered expected outcome but the run "
                    f"{matched.removeprefix('action matched; run ')}")
        tail = f"{actual} {how}, {writes} Executor write{'' if writes == '1' else 's'}, {state}, {said}, labels {labels}"
        if groups and groups[-1][:2] == [head, tail]:
            groups[-1][2] += 1
        else:
            groups.append([head, tail, 1])
    return "; ".join(f"{head}{f' x{count}' if count > 1 else ''}: {tail}" for head, tail, count in groups) \
        or "no scenario runs"


DERIVATIONS = {
    "pass_ratio": pass_ratio,
    "zero_write_rejections": zero_write_rejections,
    "chaos_injection": chaos_injection,
    "guard_unsafe_acceptance": guard_unsafe_acceptance,
    "fail_closed": fail_closed,
    "preflight_stops": preflight_stops,
    "wedge_resolutions": wedge_resolutions,
    "recovered_ratio": recovered_ratio,
    "expected_escalations": expected_escalations,
    "approval_refusals": approval_refusals,
    "injection_free": injection_free,
    "scenario_outcomes": scenario_outcomes,
    "distributed_expectations": distributed_expectations,
    "seal_outcomes": seal_outcomes,
    "trace_shapes": trace_shapes,
    "admission_probes": admission_probes,
    "container_readiness": container_readiness,
    "container_health": container_health,
    "lab_sessions": lab_sessions,
}


# ----- rendering -----

def table(headers, rows, empty=NONE):
    if not rows:
        return empty
    escape = lambda cell: str(cell).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    lines += ["| " + " | ".join(escape(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


def runs_table(bundles):
    rows = []
    for run_class in sorted({b.run_class for b in bundles}):
        members = [b for b in bundles if b.run_class == run_class]
        outcomes = [outcome(b) for b in members]
        recorded = sum(b.entry.get("provenance") == "recorded" for b in members)
        rows.append([run_class, str(len(members))] + [str(outcomes.count(o)) for o in OUTCOMES]
                    + [f"{recorded} recorded, {len(members) - recorded} inferred"])
    return table(["Class", "Runs", *OUTCOMES, "Provenance"], rows)


def load_claims(root):
    path = Path(root) / "evidence/claims.json"
    return json.loads(path.read_text(encoding="utf-8"))["claims"] if path.exists() else []


def cited(claim, bundles):
    by_id = {b.bundle_id: b for b in bundles}
    return [by_id[bundle_id] for bundle_id in claim.get("bundles", []) if bundle_id in by_id]


def claims_table(claims, bundles):
    rows = [[claim["id"], claim["text"].replace("{value}", derive(claim, bundles)), claim["scope_caveat"],
             str(len(claim.get("bundles", [])))] for claim in claims]
    return table(["Claim", "Statement", "Scope caveat", "Bundles"], rows, "_No claims yet._")


def derive(claim, bundles):
    function = DERIVATIONS.get(claim.get("derive"))
    return function(cited(claim, bundles)) if function else "<unknown derivation>"


def index_table(root):
    entries = load_index(root)["bundles"]
    rows = []
    for run_class in sorted({e["class"] for e in entries}):
        members = [e for e in entries if e["class"] == run_class]
        count = lambda key, value: str(sum(e.get(key) == value for e in members))
        rows.append([run_class, str(len(members)), count("provenance", "recorded"), count("provenance", "inferred"),
                     count("status", "superseded"), count("status", "withdrawn")])
    return table(["Class", "Bundles", "Recorded", "Inferred", "Superseded", "Withdrawn"], rows)


def exclusions_table(root):
    rows = [[entry.get("run"), entry.get("class"), short(entry.get("source_revision")), entry.get("attempted_at") or
             "unknown", entry.get("reason")] for entry in load_exclusions(root)]
    return table(["Run", "Class", "Revision", "Attempted at", "Reason not collected"], rows, "_No excluded runs._")


def limitation_blocks(root):
    """Register blocks; a missing register renders as empty, a malformed one is an error."""
    registry = {}
    if (Path(root) / limitations.REGISTER).exists():
        try:
            registry = limitations.load(root)
        except limitations.RegisterError as error:
            raise EvidenceError(str(error)) from error
        errors, _ = limitations.validate(registry, root)
        if errors:
            raise EvidenceError(limitations.REGISTER + ": " + "; ".join(errors))
    return {"limitations": limitations.render(registry), "limitations-summary": limitations.render_summary(registry)}


def quote(claim, bundles):
    statement = claim["text"].replace("{value}", derive(claim, bundles))
    return f"> **{claim['id']}:** {statement}.\n> Scope: {claim['scope_caveat']}"


def apply_quotes(text, claims, bundles, label):
    """Fill every 'claim-<id>' block with that claim's current statement and caveat."""
    newline = "\r\n" if "\r\n" in text else "\n"
    by_block = {"claim-" + str(claim.get("id")).lower(): claim for claim in claims}

    def fill(match):
        name = match.group("name")
        if not name.startswith("claim-"):
            return match.group(0)
        if name not in by_block:
            raise EvidenceError(f"{label}: block {name} quotes a claim that is not in evidence/claims.json")
        return match.group(1) + quote(by_block[name], bundles) + "\n" + match.group(4)

    return BLOCK.sub(fill, text.replace("\r\n", "\n")).replace("\n", newline)


def quote_files(root):
    """Repository-relative Markdown paths that may quote claims, in a stable order."""
    found = set()
    for pattern in QUOTE_SOURCES:
        found.update(path.relative_to(root).as_posix() for path in Path(root).glob(pattern) if path.is_file())
    return sorted(found)


def stale_statements(root, claims):
    """Paragraphs that use a known-stale status phrase without citing an existing claim id."""
    known = {claim.get("id") for claim in claims}
    problems = []
    for relative in quote_files(root):
        text = (Path(root) / relative).read_text(encoding="utf-8").replace("\r\n", "\n")
        for paragraph in re.split(r"\n\s*\n", text):
            flat = " ".join(paragraph.split()).lower()
            for phrase in STALE_PHRASES:
                if phrase in flat and not set(CLAIM_ID.findall(paragraph)) & known:
                    problems.append(f"{relative}: '{phrase}' needs a claim id from evidence/claims.json "
                                    "in the same paragraph, or the statement is out of date")
    return problems


def render_blocks(root):
    bundles = load_bundles(root)
    claims = load_claims(root)
    guard = guard_rejections(bundles)
    latency, model_calls = latency_tables(load_latency(root)[1], bundles)
    return {
        **limitation_blocks(root),
        "index": index_table(root),
        "exclusions": exclusions_table(root),
        "runs": runs_table(bundles),
        "recovery-checks": table(["Class", "Check", "Passed / runs"], recovery_matrix(bundles)),
        "agent-runs": table(["Revision", "Provenance", "Runs", "RECOVERED", "Not recovered", "Model calls (median)",
                             "Model seconds (median)", "Failure classes"], agent_runs(bundles)),
        "scenario-runs": table(["Bundle", "Scenario", "Revision", "Outcome", "Decision", "How decided",
                                "Executor writes", "Coordinator state", "Matched pre-registered outcome", "Labels"],
                               scenario_rows(bundles)),
        "guard-rejection": table(["Runs", "Refused", "Executor writes for the refused action", "Unmeasured"],
                                 [[str(guard["runs"]), ratio(guard["refused"], guard["runs"]), str(guard["writes"]),
                                   str(guard["writes_unknown"])]] if guard["runs"] else []),
        "guard-eval": table(["Bundle", "Status", "Configuration", "Basis", "Unsafe ALLOW", "Unsafe REJECT",
                             "Unsafe ESCALATE", "Safe ALLOW", "False REJECT", "Errors"], guard_evaluation(bundles)),
        "executor-wedge": table(["Bundle", "Scenario", "Revision", "Provenance", "Outcome", "Reconcile result",
                                 "Reconcile passes", "Second proposal"], wedge_rows(bundles)),
        "timings": table(["Class", "Interval", "n", "Median s", "Min s", "Max s", "Source"], timings(bundles)),
        "latency": latency,
        "model-calls": model_calls,
        "chaos": table(["Fault", "Attempts", "Injected", "Cleaned up", "Healthy recovery window"], chaos(bundles)),
        "chaos-recovery": table(["Bundle", "Scenario", "Chaos", "Revision", "Outcome", "Decision", "Executor writes",
                                 "RESTART attributed", "Forced wrong actions", "Chaos cleaned up"],
                                chaos_recovery_rows(bundles)),
        "security-lab": table(["Bundle", "Outcome", "Valid mTLS quote", "Blocked probes", "Shopping after restore"],
                              security_lab(bundles)),
        "observer": table(["Bundle", "Outcome", "Detection s", "Duplicate suppression s"], observer(bundles)),
        "distributed-runs": table(["Bundle", "Mode", "Scenario", "Revision", "Outcome", "Incident states",
                                   "Committed writes", "Unobserved windows", "Failed invariants", "Duration s",
                                   "Deadline s", "Fault visible after s"], distributed_rows(bundles)),
        "kind-lab-sessions": table(["Bundle", "Revision", "Outcome", "Executor RBAC checks as expected",
                                    "Restore check"], lab_session_rows(bundles)),
        "executor-admission": table(["Bundle", "Revision", "Provenance", "Outcome", "Probes as expected", "Allowed",
                                     "Denied by policy", "Denied by RBAC", "Cleaned up"], admission_rows(bundles)),
        "worker-container": table(["Bundle", "Revision", "Outcome",
                                   "Docker-healthy samples, first healthy one to Coordinator stop",
                                   "/readyz 200 while the Coordinator was up",
                                   "Longest planning pass s", "Highest contact age s", "/readyz 503 after stop s",
                                   "Docker unhealthy after stop s", "Docker health when saved"],
                                  container_rows(bundles)),
        "claims": claims_table(claims, bundles),
    }


def apply_blocks(text, blocks, names, label):
    """Replace each named block's body; a missing marker pair is an error."""
    newline = "\r\n" if "\r\n" in text else "\n"
    text = text.replace("\r\n", "\n")
    found = {match.group("name") for match in BLOCK.finditer(text)}
    missing = [name for name in names if name not in found]
    if missing:
        raise EvidenceError(f"{label}: missing generated markers for {', '.join(missing)}")
    text = BLOCK.sub(lambda m: m.group(1) + blocks[m.group("name")] + "\n" + m.group(4)
                     if m.group("name") in names else m.group(0), text)
    return text.replace("\n", newline)


def check_claims(root, bundles=None):
    """Problems with the claims registry; empty when every claim is supported and current."""
    bundles = load_bundles(root) if bundles is None else bundles
    entries = {e["bundle_id"]: e for e in load_index(root)["bundles"]}
    problems, seen = [], set()
    for claim in load_claims(root):
        identifier = claim.get("id")
        if not identifier or identifier in seen:
            problems.append(f"{identifier}: claim id is missing or duplicated")
        seen.add(identifier)
        if not str(claim.get("scope_caveat") or "").strip():
            problems.append(f"{identifier}: scope_caveat is required")
        if not claim.get("bundles"):
            problems.append(f"{identifier}: at least one bundle is required")
        if "{value}" not in str(claim.get("text", "")):
            problems.append(f"{identifier}: text must contain {{value}}")
        if claim.get("derive") not in DERIVATIONS:
            problems.append(f"{identifier}: unknown derivation {claim.get('derive')}")
            continue
        for bundle_id in claim.get("bundles", []):
            entry = entries.get(bundle_id)
            if entry is None:
                problems.append(f"{identifier}: bundle {bundle_id} is not in the index")
            elif entry.get("status") == "withdrawn":
                problems.append(f"{identifier}: bundle {bundle_id} is withdrawn")
        actual = derive(claim, bundles)
        if claim.get("value") != actual:
            problems.append(f"{identifier}: value {claim.get('value')!r} differs from the evidence ({actual!r})")
    return problems


def update_claim_values(root, out):
    """Write each claim's derived value back to claims.json (write mode only)."""
    path = Path(root) / "evidence/claims.json"
    if not path.exists():
        return
    registry = json.loads(path.read_text(encoding="utf-8"))
    bundles = load_bundles(root)
    changed = False
    for claim in registry.get("claims", []):
        if claim.get("derive") in DERIVATIONS and claim.get("value") != derive(claim, bundles):
            claim["value"] = derive(claim, bundles)
            changed = True
    if changed:
        path.write_bytes((json.dumps(registry, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        print("Updated evidence/claims.json", file=out)


def run(root=ROOT, *, check=False, out=sys.stdout):
    root = Path(root)
    problems = verify_bundles(root)
    if problems:
        print("\n".join("FAIL: " + p for p in problems), file=out)
        return 1
    blocks = render_blocks(root)
    bundles, claims = load_bundles(root), load_claims(root)
    files = dict(TARGETS)
    for relative in quote_files(root):
        files.setdefault(relative, ())
    failed = False
    for relative, names in files.items():
        path = root / relative
        current = path.read_bytes().decode("utf-8")
        updated = apply_quotes(apply_blocks(current, blocks, names, relative), claims, bundles, relative)
        if check and updated.replace("\r\n", "\n") != current.replace("\r\n", "\n"):
            failed = True
            diff = difflib.unified_diff(current.replace("\r\n", "\n").splitlines(), updated.replace("\r\n", "\n")
                                        .splitlines(), relative + " (committed)", relative + " (generated)",
                                        lineterm="")
            print("FAIL: generated blocks in " + relative + " are stale or hand-edited", file=out)
            print("\n".join(diff), file=out)
        elif not check and updated != current:
            path.write_bytes(updated.encode("utf-8"))
            print("Updated " + relative, file=out)
    if not check:
        update_claim_values(root, out)
    claim_problems = check_claims(root) + check_latency(root) + stale_statements(root, load_claims(root))
    if claim_problems:
        failed = True
        print("\n".join("FAIL: " + p for p in claim_problems), file=out)
    if not failed:
        print("Evidence tables and claims are current" if check else "Evidence tables regenerated", file=out)
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Fail instead of writing when anything is stale")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        return run(args.root, check=args.check)
    except EvidenceError as error:
        print("FAIL: " + str(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
