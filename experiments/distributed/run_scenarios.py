"""Run fault scenarios across separate processes on one host and check invariants.

python -m experiments.distributed.run_scenarios --mode file --scenarios S1b,S4 --repeat 5
python -m experiments.distributed.run_scenarios --mode file --all --out <directory>

Each run writes <out>/<date>-<scenario>[-rN]/ with summary.json (invariants,
versions, flags), timeline.jsonl (the allowlisted log lines of every process
plus the transitions journal, in time order) and coordinator-records.json.
Nothing written holds a credential, a lease token or an absolute path.

Every run is labelled single_host: the processes share one machine, one
kernel clock and one disk. The Planner is the deterministic provider, so
every run is also labelled synthetic_planner. Kind mode accepts only the
connected kind lab context and a prepared bundle; it has not been run yet.
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
from experiments.distributed.scenarios import DEFAULT, SCENARIOS
from experiments.distributed.topology import ROOT, Topology, check_kind_context

TIMELINE_DROP = {("debug", "http.access"), ("debug", "worker.status")}


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


def run_once(name, *, mode="file", out=None, repeat=1, context=None, kind_bundle=None, keep=False):
    """Run one scenario once; return its sanitised summary."""
    scenario = SCENARIOS[name]
    stamp = datetime.now(UTC)
    work = Path(tempfile.mkdtemp(prefix=f"sentinel-{name.lower()}-"))
    topology = Topology(work / "run", mode=mode, context=context, kind_bundle=kind_bundle, **scenario.options)
    started = time.monotonic()
    notes, error = {}, None
    try:
        topology.start(observer=False)
        try:
            notes = scenario.run(topology) or {}
        except (AssertionError, TimeoutError, RuntimeError) as caught:
            error = f"{type(caught).__name__}: {caught}"
        remaining = max(1.0, scenario.deadline_seconds - (time.monotonic() - started))
        terminal = topology.wait_terminal(remaining)
        records = invariants.collect(topology, terminal_in_time=terminal,
                                     cluster_writes=topology.kind_writes() if mode == "kind" else None)
    finally:
        topology.stop()
    results = invariants.check(records)
    failed = {item["id"] for item in results if not item["passed"]}
    summary = {
        "schema": "sentinel-distributed-run-v1", "scenario": name, "title": scenario.title, "mode": mode,
        "repeat": repeat, "date": stamp.date().isoformat(), "started_at": stamp.isoformat().replace("+00:00", "Z"),
        "duration_seconds": round(time.monotonic() - started, 1),
        "single_host": True, "separate_processes": True, "synthetic_planner": True,
        "cluster": "file-backed fake (tests/support/file_cluster.py)" if mode == "file" else context,
        "tls": topology.tls, "lease_seconds": topology.lease_seconds, "deadline_seconds": scenario.deadline_seconds,
        "git_revision": git_revision(), "python": platform.python_version(), "os": platform.system(),
        "notes": notes, "harness_error": error,
        "states": sorted({row["state"] for row in records["incidents"]}),
        "committed_writes": len(records["cluster_writes"]),
        "invariants": results, "passed": not failed,
        "expected_failures": sorted(scenario.expected_failures), "known_defect": scenario.known_defect or None,
        "matches_expectation": failed == set(scenario.expected_failures) and error is None,
        "limits": "Separate OS processes on one host: one kernel clock, one disk, loopback networking. "
                  "Not separate machines; clock skew is simulated by a gated offset, not measured.",
    }
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("file", "kind"), default="file")
    parser.add_argument("--scenarios", help="Comma-separated names; default " + ",".join(DEFAULT))
    parser.add_argument("--all", action="store_true", help="Run every scenario")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path, help="Write sanitised evidence folders here")
    parser.add_argument("--context", help="Kind mode only: an allowlisted lab context")
    parser.add_argument("--kind-bundle", type=Path, help="Kind mode only: the prepared bundle directory")
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
    elif args.context or args.kind_bundle:
        parser.error("--context and --kind-bundle are for kind mode only")
    summaries = []
    for name in names:
        for index in range(1, args.repeat + 1):
            summary = run_once(name, mode=args.mode, out=args.out, repeat=index, context=args.context,
                               kind_bundle=args.kind_bundle, keep=args.keep)
            summaries.append(summary)
            failed = [item["id"] for item in summary["invariants"] if not item["passed"]]
            print(json.dumps({"scenario": name, "repeat": index, "states": summary["states"],
                              "writes": summary["committed_writes"], "failed": failed,
                              "matches_expectation": summary["matches_expectation"],
                              "seconds": summary["duration_seconds"], "error": summary["harness_error"]}), flush=True)
    return 0 if all(summary["matches_expectation"] for summary in summaries) else 1


if __name__ == "__main__":
    sys.exit(main())
