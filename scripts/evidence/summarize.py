"""Regenerate the results tables and claim values from committed evidence bundles.

Tables are written between '<!-- BEGIN GENERATED:<name> -->' and
'<!-- END GENERATED:<name> -->' markers in docs/results.md and evidence/README.md.
Withdrawn bundles are excluded. Failed and incomplete runs stay in every
denominator. With --check nothing is written: the command fails when a
generated block was hand-edited or is stale, when a bundle's files no longer
match its MANIFEST, or when a claim is unsupported or its value is out of date.
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
TARGETS = {
    "docs/results.md": ("runs", "recovery-checks", "agent-runs", "guard-rejection", "guard-eval", "timings",
                        "chaos", "security-lab", "observer", "claims"),
    "evidence/README.md": ("index",),
}
BLOCK = re.compile(r"(<!-- BEGIN GENERATED:(?P<name>[a-z0-9-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED:(?P=name) -->)",
                   re.DOTALL)
OUTCOMES = ("PASS", "FAILED", "INTERRUPTED", "INCOMPLETE")
NONE = "_No bundles yet._"
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
    summary = bundle.json("summary.json") or bundle.json("data/evidence/summary.json") or {}
    status = summary.get("status")
    return status if status in OUTCOMES else "INCOMPLETE"


def summary_of(bundle):
    return bundle.json("summary.json") or bundle.json("data/evidence/summary.json") or {}


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
                          ("not found", "model not installed"), ("escalate", "agent escalated"),
                          ("not_recovered", "not recovered"), ("awaiting_approval", "stopped awaiting approval")):
        if needle in text.lower():
            return label
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


def agent_runs(bundles):
    """Rows per source revision; every run is in the denominator."""
    groups = {}
    for bundle in bundles:
        if bundle.run_class == "agent":
            key = (short(bundle.entry.get("source_revision")), bundle.entry.get("provenance"))
            groups.setdefault(key, []).append(bundle)
    rows = []
    for (revision, provenance), members in sorted(groups.items(), key=lambda item: min(
            b.entry.get("captured_at") or "" for b in item[1])):
        outcomes = [outcome(b) for b in members]
        failures = {}
        for bundle, result in zip(members, outcomes):
            if result != "PASS":
                label = failure_class(summary_of(bundle)) if result == "FAILED" else result.lower()
                failures[label] = failures.get(label, 0) + 1
        calls = [summary_of(b).get("agent_model_calls") for b in members if outcome(b) == "PASS"]
        rows.append([revision, provenance, str(len(members)), ratio(outcomes.count("PASS"), len(members)),
                     str(len(members) - outcomes.count("PASS")), median_text(calls),
                     median_text([model_seconds(b) for b in members if outcome(b) == "PASS"]),
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


DERIVATIONS = {
    "pass_ratio": pass_ratio,
    "zero_write_rejections": zero_write_rejections,
    "chaos_injection": chaos_injection,
    "guard_unsafe_acceptance": guard_unsafe_acceptance,
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


def render_blocks(root):
    bundles = load_bundles(root)
    claims = load_claims(root)
    guard = guard_rejections(bundles)
    return {
        "index": index_table(root),
        "runs": runs_table(bundles),
        "recovery-checks": table(["Class", "Check", "Passed / runs"], recovery_matrix(bundles)),
        "agent-runs": table(["Revision", "Provenance", "Runs", "RECOVERED", "Not recovered", "Model calls (median)",
                             "Model seconds (median)", "Failure classes"], agent_runs(bundles)),
        "guard-rejection": table(["Runs", "Refused", "Executor writes for the refused action", "Unmeasured"],
                                 [[str(guard["runs"]), ratio(guard["refused"], guard["runs"]), str(guard["writes"]),
                                   str(guard["writes_unknown"])]] if guard["runs"] else []),
        "guard-eval": table(["Bundle", "Status", "Configuration", "Basis", "Unsafe ALLOW", "Unsafe REJECT",
                             "Unsafe ESCALATE", "Safe ALLOW", "False REJECT", "Errors"], guard_evaluation(bundles)),
        "timings": table(["Class", "Interval", "n", "Median s", "Min s", "Max s", "Source"], timings(bundles)),
        "chaos": table(["Fault", "Attempts", "Injected", "Cleaned up", "Healthy recovery window"], chaos(bundles)),
        "security-lab": table(["Bundle", "Outcome", "Valid mTLS quote", "Blocked probes", "Shopping after restore"],
                              security_lab(bundles)),
        "observer": table(["Bundle", "Outcome", "Detection s", "Duplicate suppression s"], observer(bundles)),
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
    failed = False
    for relative, names in TARGETS.items():
        path = root / relative
        current = path.read_bytes().decode("utf-8")
        updated = apply_blocks(current, blocks, names, relative)
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
    claim_problems = check_claims(root)
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
