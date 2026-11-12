"""Run fault scenarios across separate processes on one host and check invariants.

python -m experiments.distributed.run_scenarios --mode file --scenarios S1b,S4 --repeat 5
python -m experiments.distributed.run_scenarios --mode file --all --out <directory>
python -m experiments.distributed.run_scenarios --mode kind --context kind-sentinel-boutique-connected \
    --kind-bundle <bundle> --scenarios S1b,S8 --repeat 2 --out <directory>

Each run writes <out>/<date>-<scenario>[-rN]/ with summary.json (invariants,
versions, flags), timeline.jsonl (the allowlisted log lines of every process
plus the transitions journal, in time order) and coordinator-records.json.
Nothing written holds a credential, a lease token or an absolute path.

Every run is labelled single_host: the processes share one machine, one
kernel clock and one disk. The Planner is the deterministic provider, so
every run is also labelled synthetic_planner.

Kind mode accepts only an allowlisted lab context and a bundle prepared by
experiments/distributed/kind_bundle.py. Before each run the operator side
(kind_bundle.KindLab, the operator's kubeconfig) injects the bundle's catalog
fault at a healthy baseline and waits until it shows; after the run it puts
the baseline template back if Sentinel did not and waits until the target is
healthy. Both are recorded under kind_lab in summary.json, as operator
actions, and a failed injection or restore stops the invocation (exit 3), so
every run starts from a fresh fault on a verified baseline. The scenario
deadline is --deadline (180 s by default in kind mode; the live Verifier takes
one sample per pass), and summary.json counts the incidents opened after the
first one as duplicate_incidents instead of hiding them.
"""
import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time

from experiments.distributed import invariants
from experiments.distributed import kind_bundle as kind_lab
from experiments.distributed.scenarios import DEFAULT, SCENARIOS
from experiments.distributed.topology import ROOT, Topology, check_kind_context

TIMELINE_DROP = {("debug", "http.access"), ("debug", "worker.status")}
# Kind mode's default scenario deadline. The scenarios' own 60 s fits file mode; live, the Verifier
# takes one sample per pass and the strict health policy waits up to 90 s, so 60 s failed I3.
KIND_DEADLINE_SECONDS = 180
DEADLINE_RANGE = (30, 1800)


def git_revision():
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                                timeout=10, check=False)
    except OSError:
        return None
    return result.stdout.strip() or None


def timeline(topology, records):
    """Allowlisted log lines of every process plus the transitions journal, ordered by wall time."""
    events = []
    for name, process in topology.processes.items():
        for line in invariants.log_lines(process.stderr_path):
            if (line.get("level"), line.get("event")) in TIMELINE_DROP and line.get("outcome") in {"idle", "renewed", None}:
                continue
            events.append({"process": name, **line})
    for row in records["transitions"]:
        events.append({"process": "coordinator-journal", "ts": row["recorded_at"], "event": "journal.transition",
                       **{key: row[key] for key in ("seq", "incident_id", "action_id", "from_state", "to_state", "cause")}})
    return sorted(events, key=lambda event: str(event.get("ts")))


def duplicate_incidents(records):
    """Incidents opened after the first one, in the order the Coordinator opened them.

    One fault on one target is injected per run, so each later incident is a
    duplicate (live, the Observer saw the rollout after the rollback as a new
    failure). Their outcome stays visible: the Guard should reject them.
    """
    opened = [row["incident_id"] for row in records["transitions"] if row["from_state"] is None]
    states = {row["incident_id"]: row["state"] for row in records["incidents"]}
    later = opened[1:] + sorted(set(states) - set(opened))
    return {"count": len(later), "incidents": [{"incident_id": item, "state": states.get(item)} for item in later]}


def run_once(name, *, mode="file", out=None, repeat=1, context=None, kind_bundle=None, keep=False,
             deadline=None, lab=None):
    """Run one scenario once; return its sanitised summary.

    Kind mode needs lab (experiments/distributed/kind_bundle.py::KindLab): the fault is injected before
    the topology starts, so the harness counts writes from the faulty
    template, and the baseline is restored after it stops. deadline replaces
    the scenario's own (kind mode; KIND_DEADLINE_SECONDS when None).
    """
    scenario = SCENARIOS[name]
    if mode == "kind" and lab is None:
        raise ValueError("Kind mode injects and restores the fault through a KindLab")
    if mode == "kind":
        deadline, source = (deadline, "--deadline") if deadline is not None else (KIND_DEADLINE_SECONDS, "kind default")
    elif deadline is not None:
        raise ValueError("--deadline is for kind mode only")
    else:
        deadline, source = scenario.deadline_seconds, "scenario"
    stamp = datetime.now(UTC)
    kind = {"fault": lab.inject()} if mode == "kind" else None  # LabError: nothing started, nothing to restore.
    work = Path(tempfile.mkdtemp(prefix=f"sentinel-{name.lower()}-"))
    started = time.monotonic()
    notes, error = {}, None
    try:
        topology = Topology(work / "run", mode=mode, context=context, kind_bundle=kind_bundle, **scenario.options)
        try:
            topology.start(observer=False)
            try:
                notes = scenario.run(topology) or {}
            except (AssertionError, TimeoutError, RuntimeError) as caught:
                error = f"{type(caught).__name__}: {caught}"
            remaining = max(1.0, deadline - (time.monotonic() - started))
            terminal = topology.wait_terminal(remaining)
            records = invariants.collect(topology, terminal_in_time=terminal,
                                         cluster_writes=topology.kind_writes() if mode == "kind" else None)
        finally:
            topology.stop()
    finally:
        if kind is not None:
            kind["restore"] = lab.restore()  # After stop: the restore is never counted as a Sentinel write.
    results = invariants.check(records)
    failed = {item["id"] for item in results if not item["passed"]}
    summary = {
        "schema": "sentinel-distributed-run-v1", "scenario": name, "title": scenario.title, "mode": mode,
        "repeat": repeat, "date": stamp.date().isoformat(), "started_at": stamp.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - started, 1),
        "single_host": True, "separate_processes": True, "synthetic_planner": True,
        "cluster": "file-backed fake (tests/support/file_cluster.py)" if mode == "file" else context,
        "tls": topology.tls, "lease_seconds": topology.lease_seconds, "deadline_seconds": deadline,
        "git_revision": git_revision(), "python": platform.python_version(), "os": platform.system(),
        "notes": notes, "harness_error": error,
        "clock_offsets_simulated": dict(topology.clock_offsets), "max_clock_skew_seconds": topology.max_clock_skew,
        # theta = Coordinator clock minus worker clock, measured by each worker (Cristian's algorithm).
        "clock_offsets_measured": {row["identity"]: {
            "theta_seconds": round(row["offset"], 3), "uncertainty_seconds": round(row["uncertainty"], 3),
            "max_abs_theta_seconds": round(row["max_abs_offset"], 3), "reports": row["reports"]}
            for row in records["worker_clocks"]},
        "states": sorted({row["state"] for row in records["incidents"]}),
        "committed_writes": sum(row["marker"] != "unobserved" for row in records["cluster_writes"]),
        "unobserved_windows": sum(row["marker"] == "unobserved" for row in records["cluster_writes"]),
        "invariants": results, "passed": not failed,
        "expected_failures": sorted(scenario.expected_failures), "known_defect": scenario.known_defect or None,
        "matches_expectation": failed == set(scenario.expected_failures) and error is None,
        "limits": "Separate OS processes on one host: one kernel clock, one disk, loopback networking. "
                  "Not separate machines; clock skew is simulated by a gated offset, and the measured "
                  "offsets measure that simulation over loopback, not skew between hosts.",
    }
    if kind is not None:
        summary.update(deadline_source=source, duplicate_incidents=duplicate_incidents(records),
                       observer_failure_window_seconds=topology.observer_failure_seconds,
                       kind_lab={**kind, "operator_actions": "the fault injection and any operator_restore are "
                                 "lab operator writes with the operator's kubeconfig, not Sentinel writes"})
    if out is not None:
        folder = Path(out) / (f"{summary['date']}-{name}" + (f"-r{repeat}" if repeat > 1 else ""))
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        (folder / "coordinator-records.json").write_text(json.dumps(records, indent=2), encoding="utf-8")
        with (folder / "timeline.jsonl").open("w", encoding="utf-8") as stream:
            for event in timeline(topology, records):
                stream.write(json.dumps(event) + "\n")
    if not keep:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    return summary


def main(argv=None, lab=None):
    """lab: a kind_bundle.KindLab to use instead of the bundle's (tests drive it offline)."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("file", "kind"), default="file")
    parser.add_argument("--scenarios", help="Comma-separated names; default " + ",".join(DEFAULT))
    parser.add_argument("--all", action="store_true", help="Run every scenario")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, help="Write sanitised evidence folders here")
    parser.add_argument("--context", help="Kind mode only: an allowlisted lab context")
    parser.add_argument("--kind-bundle", type=Path, help="Kind mode only: the prepared bundle directory")
    parser.add_argument("--deadline", type=float,
                        help=f"Kind mode only: seconds for each run to end (default {KIND_DEADLINE_SECONDS})")
    parser.add_argument("--keep", action="store_true", help="Keep the raw run directories (they hold credentials-free logs)")
    args = parser.parse_args(argv)
    if not 1 <= args.repeat <= 50:
        parser.error("--repeat must be from 1 to 50")
    names = list(SCENARIOS) if args.all else (args.scenarios.split(",") if args.scenarios else list(DEFAULT))
    unknown = [name for name in names if name not in SCENARIOS]
    if unknown:
        parser.error("Unknown scenario: " + ", ".join(unknown))
    if args.mode == "kind":
        try:
            check_kind_context(args.context)
        except ValueError as error:
            parser.error(str(error))
        if args.kind_bundle is None:
            parser.error("Kind mode needs --kind-bundle")
        if args.deadline is not None and not DEADLINE_RANGE[0] <= args.deadline <= DEADLINE_RANGE[1]:
            parser.error(f"--deadline must be from {DEADLINE_RANGE[0]} to {DEADLINE_RANGE[1]} seconds")
        if lab is None:
            try:
                lab = kind_lab.KindLab.from_bundle(args.kind_bundle, args.context)
            except kind_lab.LabError as caught:
                parser.error(str(caught))
    elif args.context or args.kind_bundle or args.deadline is not None:
        parser.error("--context, --kind-bundle and --deadline are for kind mode only")
    summaries = []
    for name in names:
        for index in range(1, args.repeat + 1):
            try:
                summary = run_once(name, mode=args.mode, out=args.out, repeat=index, context=args.context,
                                   kind_bundle=args.kind_bundle, keep=args.keep, deadline=args.deadline, lab=lab)
            except kind_lab.LabError as caught:
                # No run starts without a fresh fault on a verified baseline.
                print(json.dumps({"scenario": name, "repeat": index, "stopped": str(caught)}), flush=True)
                return 3
            summaries.append(summary)
            failed = [item["id"] for item in summary["invariants"] if not item["passed"]]
            print(json.dumps({"scenario": name, "repeat": index, "states": summary["states"],
                              "writes": summary["committed_writes"], "failed": failed,
                              "matches_expectation": summary["matches_expectation"],
                              "seconds": summary["duration_seconds"], "error": summary["harness_error"]}), flush=True)
            if args.mode == "kind" and not summary["kind_lab"]["restore"]["healthy"]:
                print(json.dumps({"scenario": name, "repeat": index,
                                  "stopped": "The target did not return to its healthy baseline after the run"}),
                      flush=True)
                return 3
    return 0 if all(summary["matches_expectation"] for summary in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
