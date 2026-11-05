"""Compare Guard gates offline on versioned fixtures; never authorize execution."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.guard.core import assess_state_policy, assert_decision_usable, evaluate
from services.guard.common import json_sha256, utc_timestamp


DEFAULT_CASES = ROOT / "data/experimentation/guard/v1/cases.json"
CONFIGURATIONS = {
    "state_policy_only": {
        "enabled_gates": ["target_allowlist", "action_allowlist_and_implementation",
                          "live_target_identity", "resource_version", "live_state_freshness"],
        "disabled_gates": ["baseline_identity_and_hash", "rollback_changes_template",
                           "baseline_health_and_references", "security", "resources_and_workload",
                           "dependency_identity_status_freshness", "operator_approval",
                           "decision_binding_and_expiry", "server_admission_dry_run", "runtime_preview"],
        "authority": "analysis_only; this comparator does not produce an execution decision",
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
    },
}


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
    paths = [ROOT / "scripts/evaluate_guard.py", ROOT / "shared/contracts.py", Path(cases_path)]
    paths += sorted((ROOT / "services/guard").glob("*.py"))
    sources = {(path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else "case_suite.json"):
               hashlib.sha256(path.read_bytes()).hexdigest()
               for path in paths}
    return {"revision": git("rev-parse", "HEAD"),
            "working_tree_dirty": bool(git("status", "--porcelain")),
            "source_sha256": sources, "python": platform.python_version(),
            "platform": platform.system(), "architecture": platform.machine()}


def run_evaluation(cases_path=DEFAULT_CASES, *, repeats=30, warmups=2):
    if type(repeats) is not int or repeats < 1 or type(warmups) is not int or warmups < 0:
        raise ValueError("Use positive repeats and nonnegative warmups")
    suite, cases = load_cases(cases_path)
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
                except Exception as exc:
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
        "suite_sha256": json_sha256(suite), "status": "PRELIMINARY_NOT_FROZEN",
        "release_prerequisite": "GitHub issue #63 remains open; rerun on the accepted frozen release before final scoring.",
        "evidence_kind": "synthetic_fixture", "generated_at": datetime.now(timezone.utc).isoformat(),
        "provenance": provenance(cases_path),
        "execution": {"cluster_contacted": False, "repair_writes_attempted": 0},
        "method": {
            "accuracy_unit": "one versioned case; repeats do not increase accuracy denominators",
            "unsafe_definition": "must not be authorized with the supplied evidence and approval, including unknown inputs",
            "rejection_definition": "REJECT only; ESCALATE is reported separately as non-authorization",
            "error_definition": "evaluation exception; retained in denominators, never credited as refusal",
            "latency_scope": "in-process evaluator only; full includes report/binding creation and ALLOW usability check; excludes fixture parsing/copying, transport and producer I/O",
            "p95_method": "nearest rank", "warmups_per_case_configuration": warmups,
            "measured_repeats_per_case_configuration": repeats,
        },
        "limitations": [
            "Hand-authored fixture coverage is not an estimate of production incident frequency or generalization.",
            "Verifier integration evidence is separate; these dependency records, health evidence and approvals are fabricated.",
            "No admission dry-run or runtime preview is included; admission acceptance cannot prove recovery health.",
            "No recovery, Kubernetes health, network latency or distributed reliability was measured.",
            "State/policy-only excludes baseline integrity, dependencies and approval; its ALLOW means comparator acceptance only.",
        ],
        "configurations": {name: {**configuration, "summary": summarize(rows[name], latencies[name]),
                                  "cases": rows[name]} for name, configuration in CONFIGURATIONS.items()},
        "expectations_met": all(row["expectation_met"] and row["consistent_across_repeats"]
                                and row["verdict"] != "ERROR" for group in rows.values() for row in group),
    }


def markdown_report(result):
    provenance_record = result["provenance"]
    lines = ["# Guard validation comparison (preliminary)", "",
             f"Suite `{result['suite_version']}`; **synthetic fixtures, not frozen-release or live evidence**.",
             "", result["release_prerequisite"], "",
             f"Evaluated revision: `{provenance_record.get('revision', 'unavailable')}`; "
             f"working tree dirty: `{provenance_record.get('working_tree_dirty', 'unavailable')}`. "
             f"Python {provenance_record.get('python', 'unavailable')} on {provenance_record.get('platform', 'unavailable')}. "
             f"{result['method']['measured_repeats_per_case_configuration']} measured repetitions and "
             f"{result['method']['warmups_per_case_configuration']} warmups per case/configuration.", "",
             "| Configuration | Unsafe REJECT | Unsafe ESCALATE | Unsafe ALLOW | Safe ALLOW | False REJECT | Median / p95 ms |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, configuration in result["configurations"].items():
        summary = configuration["summary"]
        safe, unsafe = summary["verdict_counts"]["SAFE"], summary["verdict_counts"]["UNSAFE"]
        timing = summary["latency"]
        lines.append(f"| {name} | {unsafe['REJECT']}/{summary['unsafe_cases']} | {unsafe['ESCALATE']}/{summary['unsafe_cases']} | {unsafe['ALLOW']}/{summary['unsafe_cases']} | {safe['ALLOW']}/{summary['safe_cases']} | {safe['REJECT']}/{summary['safe_cases']} | {timing['median_ms']:.3f} / {timing['p95_ms']:.3f} |")
    lines += ["", "Each accuracy denominator counts unique cases once. ESCALATE is a refusal to authorize, "
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--repeats", type=int, default=30)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output", type=Path, help="New result directory; existing paths are refused")
    arguments = parser.parse_args()
    try:
        result = run_evaluation(arguments.cases, repeats=arguments.repeats, warmups=arguments.warmups)
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
