"""Compare Guard gates offline on versioned fixtures; never authorize execution."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guard.core import assess_state_policy, assert_decision_usable, evaluate
from services.guard.common import json_sha256, utc_timestamp
from shared import log
from shared import provenance as shared_provenance

LOG = log.get_logger("guard_evaluation")


DEFAULT_CASES = ROOT / "data/experimentation/guard/v1/cases.json"
# Issue #63 freezes the evaluated release. A result is FROZEN_RELEASE only when a
# release manifest accepted under that issue names the exact clean revision that
# was evaluated. Anything else stays preliminary and says why.
FREEZE_ISSUE = 63
FROZEN = "FROZEN_RELEASE"
PRELIMINARY = "PRELIMINARY_NOT_FROZEN"
MANIFEST_FIELDS = ("tag", "commit", "accepted_issue", "accepted_at")
_COMMIT = re.compile(r"[0-9a-f]{40}")
ALL_GATES = ["target_allowlist", "action_allowlist_and_implementation", "live_target_identity", "resource_version",
             "live_state_freshness", "baseline_identity_and_hash", "rollback_changes_template",
             "baseline_health_and_references", "security", "resources_and_workload",
             "dependency_identity_status_freshness", "operator_approval", "decision_binding_and_expiry",
             "server_admission_dry_run", "runtime_preview"]
CONFIGURATIONS = {
    "state_policy_only": {
        "enabled_gates": ["target_allowlist", "action_allowlist_and_implementation",
                          "live_target_identity", "resource_version", "live_state_freshness"],
        "disabled_gates": ["baseline_identity_and_hash", "rollback_changes_template",
                           "baseline_health_and_references", "security", "resources_and_workload",
                           "dependency_identity_status_freshness", "operator_approval",
                           "decision_binding_and_expiry", "server_admission_dry_run", "runtime_preview"],
        "authority": "analysis_only; this comparator does not produce an execution decision",
        "basis": "measured",
    },
    "full_available": {
        "enabled_gates": ["target_allowlist", "action_allowlist_and_implementation",
                          "live_target_identity", "resource_version", "live_state_freshness",
                          "baseline_identity_and_hash", "rollback_changes_template",
                          "baseline_health_and_references", "security", "resources_and_workload",
                          "dependency_identity_status_freshness", "operator_approval",
                          "decision_binding_and_expiry"],
        "disabled_gates": ["server_admission_dry_run", "runtime_preview"],
        "authority": "fabricated fixture inputs only; never use these decisions for live execution",
        "basis": "measured",
    },
    # Anchor row for "act on the model's proposal without validation". Every
    # case is ALLOW by construction, so its counts are definitional, not measured.
    "no_validation": {
        "enabled_gates": [],
        "disabled_gates": list(ALL_GATES),
        "authority": "analysis_only; definitional comparator that allows every proposal",
        "basis": "definitional",
    },
}


def _validated_manifest(manifest):
    if not isinstance(manifest, dict) or any(field not in manifest for field in MANIFEST_FIELDS):
        raise ValueError("Release manifest must be a JSON object with " + ", ".join(MANIFEST_FIELDS))
    if not isinstance(manifest["tag"], str) or not manifest["tag"].strip():
        raise ValueError("Release manifest tag must be a non-empty string")
    if not isinstance(manifest["commit"], str) or not _COMMIT.fullmatch(manifest["commit"]):
        raise ValueError("Release manifest commit must be a full 40-character lower-case hex revision")
    if type(manifest["accepted_issue"]) is not int or manifest["accepted_issue"] != FREEZE_ISSUE:
        raise ValueError(f"Release manifest must record acceptance under issue #{FREEZE_ISSUE}")
    if not isinstance(manifest["accepted_at"], str):
        raise ValueError("Release manifest accepted_at must be an ISO 8601 UTC timestamp")
    utc_timestamp(manifest["accepted_at"], "accepted_at")
    return {field: manifest[field] for field in MANIFEST_FIELDS}


def release_status(provenance_record, manifest_path=None):
    """Compute the freeze status from the issue #63 conditions; never assume a freeze.

    FROZEN_RELEASE needs an accepted release manifest whose commit is the exact
    evaluated revision and a clean working tree. Otherwise the result is
    PRELIMINARY_NOT_FROZEN with the first unmet condition as the reason. A
    malformed manifest raises ValueError instead of being read as preliminary.
    """
    manifest = None
    if manifest_path is not None:
        try:
            loaded = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Release manifest is not valid JSON") from exc
        manifest = _validated_manifest(loaded)
    revision = (provenance_record or {}).get("revision")
    dirty = (provenance_record or {}).get("working_tree_dirty")
    if manifest is None:
        reason = "no_manifest"
    elif not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
        reason = "revision_unknown"
    elif manifest["commit"] != revision:
        reason = "commit_mismatch"
    elif dirty is not False:
        reason = "dirty_tree"
    else:
        reason = None
    status = FROZEN if reason is None else PRELIMINARY
    if status == FROZEN:
        prerequisite = (f"Frozen release {manifest['tag']} at {manifest['commit']}, accepted under issue "
                        f"#{FREEZE_ISSUE} at {manifest['accepted_at']}.")
    else:
        prerequisite = (f"GitHub issue #{FREEZE_ISSUE} freeze conditions are not met ({reason}); rerun on the "
                        "accepted frozen release before final scoring.")
    return {"status": status, "reason": reason, "freeze_issue": FREEZE_ISSUE, "manifest": manifest,
            "release_prerequisite": prerequisite}


def load_cases(path):
    """Materialize both configurations from the same immutable case definition."""
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    if suite["schema_version"] != "1.0" or suite["evidence_kind"] != "synthetic_fixture":
        raise ValueError("Only version-1 synthetic fixture suites are supported")
    cases, seen = [], set()
    for specification in suite["cases"]:
        case_id = specification["id"]
        if case_id in seen or specification["label"] not in {"SAFE", "UNSAFE"}:
            raise ValueError("Case IDs must be unique and labels must be SAFE or UNSAFE")
        seen.add(case_id)
        values = deepcopy(suite["base_inputs"])
        for change in specification["changes"]:
            location = values
            path_parts = change["path"]
            if not path_parts:
                raise ValueError("Changes must name an input field")
            for part in path_parts[:-1]:
                location = location[part]
            location[path_parts[-1]] = deepcopy(change["value"])
        # This suite deliberately excludes even fabricated admission previews.
        if values.get("preview") is not None or values["policy"].get("require_server_dry_run"):
            raise ValueError("Admission dry-run is outside this fixture comparison")
        cases.append((specification, values))
    if not cases or {item[0]["label"] for item in cases} != {"SAFE", "UNSAFE"}:
        raise ValueError("The suite must contain safe and unsafe cases")
    return suite, cases


def evaluate_configuration(configuration, values):
    if configuration == "no_validation":
        return {"verdict": "ALLOW", "reason_codes": ["NO_VALIDATION"]}
    if configuration == "state_policy_only":
        reasons = assess_state_policy(values["proposal"], values["live_state"],
                                      values["policy"], now=values["now"])
        return {"verdict": "REJECT" if reasons else "ALLOW",
                "reason_codes": reasons or ["STATE_POLICY_PASSED"]}
    if configuration != "full_available":
        raise ValueError("Unknown comparison configuration")
    decision = evaluate(**values)
    if decision["verdict"] == "ALLOW":
        assert_decision_usable(decision, **values)
    return {key: decision[key] for key in ("verdict", "reason_codes")}


def latency_summary(samples):
    ordered = sorted(samples)
    return {"samples": len(ordered), "median_ms": statistics.median(ordered),
            "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
            "min_ms": ordered[0], "max_ms": ordered[-1]}


def ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def summarize(rows, latencies):
    safe = [row for row in rows if row["label"] == "SAFE"]
    unsafe = [row for row in rows if row["label"] == "UNSAFE"]
    count = lambda selected, verdict: sum(row["verdict"] == verdict for row in selected)
    return {
        "safe_cases": len(safe), "unsafe_cases": len(unsafe),
        "verdict_counts": {label: {verdict: count(selected, verdict)
                                  for verdict in ("ALLOW", "REJECT", "ESCALATE", "ERROR")}
                           for label, selected in (("SAFE", safe), ("UNSAFE", unsafe))},
        "unsafe_action_rejection": ratio(count(unsafe, "REJECT"), len(unsafe)),
        "unsafe_non_authorization": ratio(count(unsafe, "REJECT") + count(unsafe, "ESCALATE"), len(unsafe)),
        "unsafe_acceptance": ratio(count(unsafe, "ALLOW"), len(unsafe)),
        "false_rejection": ratio(count(safe, "REJECT"), len(safe)),
        "safe_non_authorization": ratio(count(safe, "REJECT") + count(safe, "ESCALATE"), len(safe)),
        "safe_acceptance": ratio(count(safe, "ALLOW"), len(safe)),
        "errors": count(rows, "ERROR"),
        "latency": latency_summary(latencies),
    }


def provenance(cases_path):
    def git(*arguments):
        completed = subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True,
                                   text=True, timeout=10, check=True)
        return completed.stdout.strip()
    paths = [ROOT / "scripts/evaluate_guard.py", ROOT / "shared/contracts.py", ROOT / "shared/canonical.py",
             Path(cases_path)]
    paths += sorted((ROOT / "services/guard").glob("*.py"))
    sources = {(path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else "case_suite.json"):
               hashlib.sha256(path.read_bytes()).hexdigest()
               for path in paths}
    # The shared block lets the evidence collector mark this result 'recorded'.
    return {**shared_provenance.collect(ROOT, captured_by="evaluate_guard"),
            "revision": git("rev-parse", "HEAD"),
            "working_tree_dirty": bool(git("status", "--porcelain")),
            "source_sha256": sources, "python": platform.python_version(),
            "platform": platform.system(), "architecture": platform.machine()}


def run_evaluation(cases_path=DEFAULT_CASES, *, repeats=30, warmups=2, release_manifest=None):
    if type(repeats) is not int or repeats < 1 or type(warmups) is not int or warmups < 0:
        raise ValueError("Use positive repeats and nonnegative warmups")
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    suite, cases = load_cases(cases_path)
    provenance_record = provenance(cases_path)
    release = release_status(provenance_record, release_manifest)
    rows = {name: [] for name in CONFIGURATIONS}
    latencies = {name: [] for name in CONFIGURATIONS}
    for specification, serialized_inputs in cases:
        input_digest = json_sha256(serialized_inputs)
        values = deepcopy(serialized_inputs)
        values["now"] = utc_timestamp(values["now"], "now")
        trials = {name: [] for name in CONFIGURATIONS}
        timings = {name: [] for name in CONFIGURATIONS}
        for trial in range(-warmups, repeats):
            # Alternate ordering; copies and clock parsing are outside timing.
            names = list(CONFIGURATIONS)
            if trial % 2:
                names.reverse()
            for name in names:
                candidate = deepcopy(values)
                started = perf_counter_ns()
                try:
                    result = evaluate_configuration(name, candidate)
                except Exception as exc:  # noqa: BLE001 - a failed trial is scored as ERROR, never as a refusal
                    log.failure(LOG, "guard_evaluation.trial.failed", exc)
                    # Errors are never scored as successful refusals. Do not echo input data.
                    result = {"verdict": "ERROR", "reason_codes": [type(exc).__name__]}
                elapsed = (perf_counter_ns() - started) / 1_000_000
                if trial >= 0:
                    trials[name].append(result)
                    timings[name].append(elapsed)
        for name in CONFIGURATIONS:
            result = trials[name][0]
            consistent = all(item == result for item in trials[name])
            verdict_counts = {verdict: sum(item["verdict"] == verdict for item in trials[name])
                              for verdict in ("ALLOW", "REJECT", "ESCALATE", "ERROR")}
            if not consistent or verdict_counts["ERROR"]:
                result = {"verdict": "ERROR", "reason_codes": ["INCONSISTENT_OR_FAILED_TRIAL"]}
            expected = specification["expected_full"] if name == "full_available" else None
            matches = expected is None or (result["verdict"] == expected["verdict"]
                                           and expected["reason"] in result["reason_codes"])
            rows[name].append({"case_id": specification["id"], "category": specification["category"],
                               "label": specification["label"], "case_inputs_sha256": input_digest,
                               **result, "consistent_across_repeats": consistent,
                               "trial_verdict_counts": verdict_counts,
                               "expectation_met": matches, "latency": latency_summary(timings[name])})
            latencies[name].extend(timings[name])
    return {
        "schema_version": "1.0", "suite_version": suite["suite_version"],
        "suite_sha256": json_sha256(suite), "status": release["status"],
        "release_prerequisite": release["release_prerequisite"],
        "release": {key: release[key] for key in ("status", "reason", "freeze_issue", "manifest")},
        "evidence_kind": "synthetic_fixture", "started_at": started_at,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance_record,
        "execution": {"cluster_contacted": False, "repair_writes_attempted": 0},
        "method": {
            "accuracy_unit": "one versioned case; repeats do not increase accuracy denominators",
            "unsafe_definition": "must not be authorized with the supplied evidence and approval, including unknown inputs",
            "rejection_definition": "REJECT only; ESCALATE is reported separately as non-authorization",
            "error_definition": "evaluation exception; retained in denominators, never credited as refusal",
            "latency_scope": "in-process evaluator only; full includes report/binding creation and ALLOW usability check; excludes fixture parsing/copying, transport and producer I/O",
            "p95_method": "nearest rank", "warmups_per_case_configuration": warmups,
            "measured_repeats_per_case_configuration": repeats,
            "definitional_rows": "no_validation allows every case by construction; its counts are not a measurement",
        },
        "limitations": [
            "Hand-authored fixture coverage is not an estimate of production incident frequency or generalization.",
            "Verifier integration evidence is separate; these dependency records, health evidence and approvals are fabricated.",
            "No admission dry-run or runtime preview is included; admission acceptance cannot prove recovery health.",
            "No recovery, Kubernetes health, network latency or distributed reliability was measured.",
            "State/policy-only excludes baseline integrity, dependencies and approval; its ALLOW means comparator acceptance only.",
            "No-validation is definitional: it allows every case by construction and measures nothing.",
        ],
        "configurations": {name: {**configuration, "summary": summarize(rows[name], latencies[name]),
                                  "cases": rows[name]} for name, configuration in CONFIGURATIONS.items()},
        "expectations_met": all(row["expectation_met"] and row["consistent_across_repeats"]
                                and row["verdict"] != "ERROR" for group in rows.values() for row in group),
    }


def markdown_report(result):
    provenance_record = result["provenance"]
    frozen = result["status"] == FROZEN
    lines = ["# Guard validation comparison (" + ("frozen release" if frozen else "preliminary") + ")", "",
             f"Suite `{result['suite_version']}`; **synthetic fixtures, not "
             + ("live evidence" if frozen else "frozen-release or live evidence") + "**.",
             "", f"Status: `{result['status']}`. " + result["release_prerequisite"], "",
             f"Evaluated revision: `{provenance_record.get('revision', 'unavailable')}`; "
             f"working tree dirty: `{provenance_record.get('working_tree_dirty', 'unavailable')}`. "
             f"Python {provenance_record.get('python', 'unavailable')} on {provenance_record.get('platform', 'unavailable')}. "
             f"{result['method']['measured_repeats_per_case_configuration']} measured repetitions and "
             f"{result['method']['warmups_per_case_configuration']} warmups per case/configuration.", "",
             "| Configuration | Basis | Unsafe REJECT | Unsafe ESCALATE | Unsafe ALLOW | Safe ALLOW | False REJECT | Median / p95 ms |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, configuration in result["configurations"].items():
        summary = configuration["summary"]
        safe, unsafe = summary["verdict_counts"]["SAFE"], summary["verdict_counts"]["UNSAFE"]
        timing = summary["latency"]
        lines.append(f"| {name} | {configuration.get('basis', 'measured')} | {unsafe['REJECT']}/{summary['unsafe_cases']} | {unsafe['ESCALATE']}/{summary['unsafe_cases']} | {unsafe['ALLOW']}/{summary['unsafe_cases']} | {safe['ALLOW']}/{summary['safe_cases']} | {safe['REJECT']}/{summary['safe_cases']} | {timing['median_ms']:.3f} / {timing['p95_ms']:.3f} |")
    lines += ["", "`state_policy_only` and `full_available` are measured. `no_validation` is definitional: it "
              "allows every case by construction, so its counts are not a measurement and its timing is only "
              "the cost of returning a constant.",
              "", "Each accuracy denominator counts unique cases once. ESCALATE is a refusal to authorize, "
              "and is not counted as REJECT. Errors remain in the denominators and receive no credit.",
              "", "Latency measures only the in-process checks named in JSON, including full report construction "
              "and ALLOW binding/expiry validation. It excludes producer, API and transport time. "
              "Timing repetitions do not create independent accuracy samples.", "",
              "Enabled and disabled gates, input/source hashes, per-case reasons, counts, timings and all limitations "
              "are recorded in [results.json](results.json). No admission or runtime preview is scored. "
              "These results make no claim about recovered application health.", "",
              "## Case decisions", "",
              "| Case | Label | State/policy | Full validation | Full reason |",
              "| --- | --- | --- | --- | --- |"]
    state_cases = {row["case_id"]: row for row in result["configurations"]["state_policy_only"]["cases"]}
    for row in result["configurations"]["full_available"]["cases"]:
        lines.append(f"| {row['case_id']} | {row['label']} | {state_cases[row['case_id']]['verdict']} | {row['verdict']} | {', '.join(row['reason_codes'])} |")
    lines.append("")
    return "\n".join(lines)


def verdict_differences(committed, current):
    """Where a committed result no longer matches a fresh run; timings are ignored.

    Compares the suite hash, the configuration names and every case's verdict
    and reason codes. Returns readable lines; an empty list means still current.
    """
    problems = []
    if committed.get("suite_sha256") != current.get("suite_sha256"):
        problems.append("suite_sha256 differs: the case suite changed")
    committed_configurations = committed.get("configurations") or {}
    current_configurations = current.get("configurations") or {}
    if set(committed_configurations) != set(current_configurations):
        problems.append("configurations differ: committed " + ", ".join(sorted(committed_configurations))
                        + "; current " + ", ".join(sorted(current_configurations)))
    for name in sorted(set(committed_configurations) & set(current_configurations)):
        before = {row["case_id"]: row for row in committed_configurations[name].get("cases", [])}
        after = {row["case_id"]: row for row in current_configurations[name].get("cases", [])}
        for case_id in sorted(set(before) | set(after)):
            old, new = before.get(case_id), after.get(case_id)
            if old is None or new is None:
                problems.append(f"{name}/{case_id}: case is only in the {'current' if old is None else 'committed'} run")
            elif (old["verdict"], old["reason_codes"]) != (new["verdict"], new["reason_codes"]):
                problems.append(f"{name}/{case_id}: committed {old['verdict']} {old['reason_codes']}, "
                                f"current {new['verdict']} {new['reason_codes']}")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output", type=Path, help="New result directory; existing paths are refused")
    parser.add_argument("--release-manifest", type=Path,
                        help=f"JSON {{tag, commit, accepted_issue, accepted_at}} accepted under issue #{FREEZE_ISSUE}; "
                             "without it, results are PRELIMINARY_NOT_FROZEN")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="Write --output even when the working tree has uncommitted changes")
    arguments = parser.parse_args()
    try:
        result = run_evaluation(arguments.cases, repeats=arguments.repeats, warmups=arguments.warmups,
                                release_manifest=arguments.release_manifest)
        if arguments.output is not None and result["provenance"].get("working_tree_dirty") is not False \
                and not arguments.allow_dirty:
            raise ValueError("Refusing --output: the working tree is dirty or its state is unknown, so the "
                             "recorded revision would not match the evaluated code. Commit first or pass --allow-dirty.")
        if arguments.output is not None:
            arguments.output.mkdir(parents=True, exist_ok=False)
            (arguments.output / "results.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            (arguments.output / "README.md").write_text(markdown_report(result), encoding="utf-8")
        else:
            print(json.dumps(result, indent=2, allow_nan=False))
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    return 0 if result["expectations_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
