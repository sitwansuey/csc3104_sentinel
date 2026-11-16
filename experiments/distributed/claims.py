"""Claim throughput, time to the first claim after a Coordinator restart, and a restore drill (P3.06).

python -m experiments.distributed.claims rate --backlog 50,200 --clients 6
python -m experiments.distributed.claims restart --repeat 5
python -m experiments.distributed.claims restart --repeat 5 --poll-seconds 2 --down-seconds 30
python -m experiments.distributed.claims restore

Both measurements use the file mode of the multi-process topology
(experiments/distributed/topology.py): the Coordinator is its own process on
127.0.0.1, and nothing touches a Kubernetes cluster.

rate seeds the Coordinator's outbox with synthetic verifier and executor
commands that carry no action, then lets client threads in this process claim
them over HTTP(S) as verifier-1, executor-a and executor-b until none is left.
It reports claims per second and the latency of each claim. Each claim runs a
full outbox replay inside one BEGIN IMMEDIATE transaction
(runtime/worker_transport.py::Delivery.transaction), so the rate falls as the
backlog grows. The commands are never processed: this measures the claim path
alone, not end-to-end recovery.

restart runs scenario S5 (scenarios.py::s5): the Coordinator is killed after
it commits an execution result and before it replies, restarted, and restarted
again once the action is verifying. With --down-seconds it stays down that
long before each restart. For each restart it reports, per worker, the seconds
from the restarted Coordinator answering /readyz to that worker's first
successful claim, read from the Coordinator's access log (one host, one
clock). The numbers depend on --poll-seconds and on the worker back-off in
runtime/worker_service.py, which the output names.

restore runs the whole flow, copies the Coordinator database (SQLite backup
API) when the action is READY, lets the action finish, then kills the
Coordinator, puts the copy back and starts it. It records how soon each
worker turns unready with store_changed, what
scripts/coordinator_restore_check.py lists, and whether the run ends again
after every worker is cleared with worker_service --accept-store, still with
one template write.

These are processes on one host sharing one kernel clock, one disk and
loopback networking. They are not separate machines, and the numbers say
nothing about a Coordinator Pod restart in a cluster.
"""
import argparse
from contextlib import suppress
from datetime import UTC, datetime
import http.client
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

from experiments.distributed import invariants
from experiments.distributed.run_scenarios import git_revision
from experiments.distributed.topology import LOOPBACK, ROOT, TERMINAL, WORKERS, Topology
from shared import log

LOG = log.get_logger("claims_measurement")

SYNTHETIC = (("verifier-1", "verifier.request"), ("executor-a", "executor.request"),
             ("executor-b", "executor.request"))
LIMITS = ("Separate OS processes on one host: one kernel clock, one disk, loopback networking. "
          "Not separate machines, and not a Coordinator Pod restart in a cluster.")


def backoff_policy():
    """The worker back-off in effect, as runtime/worker_service.py defines it."""
    from runtime import worker_service
    cap = getattr(worker_service, "BACKOFF_CAP_SECONDS", None)
    if cap is None:
        return {"cap_seconds": 60, "jitter": None,
                "formula": "min(60, poll_seconds * 2 ** min(failures, 5))"}
    return {"cap_seconds": cap, "jitter": "full",
            "formula": f"uniform(0, min({cap}, poll_seconds * 2 ** min(failures, 5)))"}


def _labels(mode_note):
    return {"mode": "file", "single_host": True, "separate_processes": True, "loopback": LOOPBACK,
            "measured": mode_note, "git_revision": git_revision(), "python": platform.python_version(),
            "os": platform.system(), "backoff": backoff_policy(), "limits": LIMITS}


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))] if ordered else None


def _client(topology, identity):
    from runtime.worker_http import WorkerClient
    return WorkerClient(f"{topology.scheme}://{LOOPBACK}:{topology.ports['delivery']}", identity,
                        topology.tokens[identity], timeout=30, tls_context=topology._tls_context())


def seed(topology, per_topic):
    """Queue per_topic synthetic commands for each claiming topic; they name no action."""
    from services.coordinator.store import SQLiteStore
    with SQLiteStore(topology.paths["coordinator"] / "coordinator.db") as store, store.transaction():
        for topic in sorted({topic for _, topic in SYNTHETIC}):
            for index in range(per_topic):
                store.enqueue(topic, {"synthetic": index})


def measure_rate(run_dir, *, backlog, clients):
    """Claim a seeded backlog with client threads; return claims per second and latencies.

    The lease is long enough that no command is claimed twice in a run, so
    the run ends when every command has been claimed once. Claimed commands
    stay pending in the outbox (nothing finishes them), so every claim
    replays the whole backlog, as a slow worker fleet would make it do.
    """
    topology = Topology(run_dir, lease_seconds=3600)
    try:
        topology.prepare()
        topology.processes["coordinator"].start()
        topology.wait_coordinator_ready()
        per_topic = max(1, backlog // 2)
        seed(topology, per_topic)
        latencies, claimed, errors = [], {}, []
        lock, start = threading.Lock(), threading.Barrier(clients)

        def claim(identity):
            connection = _client(topology, identity)
            start.wait()
            while True:
                began = time.perf_counter()
                try:
                    delivery = connection.post("claim", {})["delivery"]
                except Exception as error:  # noqa: BLE001 - recorded by class and counted as a failed run
                    log.failure(LOG, "claims.claim.failed", error, identity=identity)
                    with lock:
                        errors.append(type(error).__name__ + (f":{error.status}" if hasattr(error, "status") else ""))
                    return
                with lock:
                    latencies.append(time.perf_counter() - began)
                    if delivery is not None:
                        claimed[identity] = claimed.get(identity, 0) + 1
                if delivery is None:
                    return

        threads = [threading.Thread(target=claim, args=(SYNTHETIC[index % len(SYNTHETIC)][0],))
                   for index in range(clients)]
        began = time.perf_counter()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - began
    finally:
        topology.stop()
    total = sum(claimed.values())
    return {"backlog": per_topic * 2, "clients": clients, "claims": total, "claims_by_identity": claimed,
            "seconds": round(elapsed, 3), "claims_per_second": round(total / elapsed, 1) if elapsed else None,
            "latency_ms": {"median": round(1000 * statistics.median(latencies), 1) if latencies else None,
                           "p95": round(1000 * _percentile(latencies, 0.95), 1) if latencies else None,
                           "max": round(1000 * max(latencies), 1) if latencies else None},
            "errors": errors, "tls": topology.tls}


def _timestamp(line):
    with suppress(KeyError, TypeError, ValueError):
        return datetime.fromisoformat(line["ts"].replace("Z", "+00:00")).timestamp()
    return None


def _restart(topology, down_seconds, timeout=60):
    """Kill the Coordinator if it runs, keep it down, start it; return wall times."""
    process = topology.processes["coordinator"]
    process.kill()
    stopped = time.time()
    time.sleep(down_seconds)
    started = time.time()
    process.start()
    topology.wait_coordinator_ready(timeout)
    return {"stopped": stopped, "started": started, "ready": time.time()}


def first_claims(lines, restarts):
    """Per restart, each worker's first successful claim after the Coordinator was ready."""
    claims = [(stamp, line) for line in lines
              if line.get("event") == "http.access" and line.get("operation") == "claim"
              and line.get("http_status") == 200 and (stamp := _timestamp(line)) is not None]
    report = []
    for index, restart in enumerate(restarts):
        end = restarts[index + 1]["stopped"] if index + 1 < len(restarts) else float("inf")
        window = [(stamp, line) for stamp, line in claims if restart["ready"] <= stamp < end]
        workers = {}
        for name, _ in WORKERS:
            stamps = [stamp for stamp, line in window if line.get("identity") == name]
            workers[name] = round(min(stamps) - restart["ready"], 3) if stamps else None
        delivered = [stamp for stamp, line in window if line.get("outcome") == "claimed"]
        seen = [value for value in workers.values() if value is not None]
        report.append({
            "down_seconds": round(restart["started"] - restart["stopped"], 3),
            "ready_after_start_seconds": round(restart["ready"] - restart["started"], 3),
            "first_claim_seconds": workers,
            "all_workers_claimed_seconds": max(seen) if len(seen) == len(workers) else None,
            "first_delivery_seconds": round(min(delivered) - restart["ready"], 3) if delivered else None})
    return report


def _wait_all_claimed(topology, restart, timeout):
    """Wait until every worker has claimed since restart was ready; False at the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = invariants.log_lines(topology.processes["coordinator"].stderr_path)
        if first_claims(lines, [restart])[0]["all_workers_claimed_seconds"] is not None:
            return True
        time.sleep(0.2)
    return False


def measure_restart(run_dir, *, poll_seconds, down_seconds, timeout=120):
    """Scenario S5 with timed restarts; return the restart report and the end state."""
    topology = Topology(run_dir, poll_seconds=poll_seconds)
    restarts, error = [], None
    try:
        topology.start(observer=False)
        stem = topology.arm("coordinator.after_commit_before_reply", "exit", qualifier="execution_result")
        topology.start_observer()
        try:
            topology.wait_reached(stem, timeout)
            deadline = time.monotonic() + 15
            while topology.processes["coordinator"].running and time.monotonic() < deadline:
                time.sleep(0.05)
            restarts.append(_restart(topology, down_seconds))
            # The action is usually VERIFYING already (the result was committed
            # before the crash), so wait for every worker's first claim first;
            # otherwise the second restart would cut the first one's window.
            _wait_all_claimed(topology, restarts[-1], timeout)
            topology.wait_state({"VERIFYING", *TERMINAL}, timeout)
            restarts.append(_restart(topology, down_seconds))
            _wait_all_claimed(topology, restarts[-1], timeout)
        except (TimeoutError, RuntimeError) as caught:
            error = f"{type(caught).__name__}: {caught}"
        terminal = topology.wait_terminal(timeout)
        states = sorted(set(topology.states()))
        lines = invariants.log_lines(topology.processes["coordinator"].stderr_path)
    finally:
        topology.stop()
    return {"poll_seconds": poll_seconds, "restarts": first_claims(lines, restarts), "states": states,
            "terminal": terminal, "harness_error": error, "tls": topology.tls}


def summarise(runs):
    """Median and maximum over runs of the slowest worker's first claim, per restart."""
    result = []
    for index in range(max((len(run["restarts"]) for run in runs), default=0)):
        values = [run["restarts"][index]["all_workers_claimed_seconds"] for run in runs
                  if len(run["restarts"]) > index and run["restarts"][index]["all_workers_claimed_seconds"] is not None]
        result.append({"restart": index + 1, "runs": len(values),
                       "median_seconds": round(statistics.median(values), 3) if values else None,
                       "max_seconds": round(max(values), 3) if values else None})
    return result


def _copy_database(source, target):
    """Copy a SQLite database with the backup API, as an operator's backup or restore would."""
    src, dst = sqlite3.connect(source), sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def _store_changed(topology, name):
    with suppress(OSError, ValueError, http.client.HTTPException):
        return topology.worker_probe(name)[1].get("store_changed") is True
    return False


def _accept_store(topology, name):
    subprocess.run([topology.python, "-m", "runtime.worker_service", "--config",
                    str(topology.paths["config"] / f"{name}.json"), "--accept-store"],
                   cwd=ROOT, env=topology._env(name), capture_output=True, check=True, timeout=60)


def restore_drill(run_dir, *, poll_seconds, staged=True, timeout=90):
    """Back up the Coordinator database at READY, let the action finish, restore the backup, recover.

    Returns when each worker turned unready with store_changed, what
    scripts/coordinator_restore_check.py reported, and how the run ended
    after every worker was cleared with worker_service --accept-store.
    """
    from scripts import coordinator_restore_check
    from tests.support.file_cluster import FileCluster
    topology = Topology(run_dir, poll_seconds=poll_seconds)
    database = topology.paths["coordinator"] / "coordinator.db"
    backup = run_dir / "backup.db"
    report = {"poll_seconds": poll_seconds}
    try:
        topology.start()
        topology.wait_state({"READY"}, timeout)
        _copy_database(database, backup)
        report["first_end"] = {"terminal": topology.wait_terminal(timeout), "states": topology.states()}
        process = topology.processes["coordinator"]
        process.kill()
        _copy_database(backup, database)
        process.start()
        topology.wait_coordinator_ready(timeout)
        restored = time.monotonic()
        report["after_restore"] = {"states": topology.states()}
        detected, deadline = {}, time.monotonic() + timeout
        while len(detected) < len(WORKERS) and time.monotonic() < deadline:
            for name, _ in WORKERS:
                if name not in detected and _store_changed(topology, name):
                    detected[name] = round(time.monotonic() - restored, 3)
            time.sleep(0.05)
        report["store_changed_seconds"] = {name: detected.get(name) for name, _ in WORKERS}
        journals = [topology.paths["state"] / name / "transport.db" for name, _ in WORKERS]
        check = coordinator_restore_check.check(database, journals)
        report["restore_check"] = {
            "may_lose_acknowledged_work": check["may_lose_acknowledged_work"], "missing": check["missing"],
            "journals": [{"journal": item["journal"], "rolled_back": item.get("rolled_back", False),
                          "missing": [{key: entry[key] for key in ("kind", "topic", "redrive")}
                                      for entry in item["missing"]]} for item in check["journals"]]}
        # Clear the workers holding replay items first, and the rest once their
        # results are back: a peer that claimed a replayed request first would
        # report a result of its own instead (staged=False clears all at once).
        replaying = [item["journal"].split("/")[0] for item in check["journals"]
                     if any(entry["redrive"] == "replay" for entry in item["missing"])]
        first = replaying if staged else [name for name, _ in WORKERS]
        report["accepted_first"] = first
        for name in first:
            _accept_store(topology, name)
        deadline = time.monotonic() + timeout
        while staged and time.monotonic() < deadline and any(
                item["missing"] for item in coordinator_restore_check.check(database, journals)["journals"]
                if item["journal"].split("/")[0] in first):
            time.sleep(0.1)
        for name, _ in WORKERS:
            if name not in first:
                _accept_store(topology, name)
        accepted = time.monotonic()
        report["second_end"] = {"terminal": topology.wait_terminal(timeout), "states": topology.states(),
                                "seconds_after_accept": round(time.monotonic() - accepted, 3)}
        with FileCluster(run_dir / "cluster.db") as cluster:
            report["template_writes"] = len(cluster.committed_writes())
        report["statuses"] = {name: sorted({record.get("status") for record in
                                            topology.processes[name].stdout_records()} - {None})
                              for name, _ in WORKERS}
    finally:
        topology.stop()
    report["tls"] = topology.tls
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    rate = commands.add_parser("rate", help="Claims per second over a seeded backlog")
    rate.add_argument("--backlog", default="50,200", help="Comma-separated backlog sizes (commands queued)")
    rate.add_argument("--clients", type=int, default=6, help="Claiming client threads")
    restart = commands.add_parser("restart", help="Time to first claim after Coordinator restarts (S5)")
    restart.add_argument("--repeat", type=int, default=5)
    restart.add_argument("--poll-seconds", type=float, default=0.2, help="Worker poll interval")
    restart.add_argument("--down-seconds", type=float, default=0, help="Keep the Coordinator down this long")
    drill = commands.add_parser("restore", help="Restore the Coordinator database from a backup mid-run")
    drill.add_argument("--repeat", type=int, default=1)
    drill.add_argument("--poll-seconds", type=float, default=0.2, help="Worker poll interval")
    drill.add_argument("--all-at-once", action="store_true",
                       help="Clear every worker together instead of the ones holding replay items first")
    for command in (rate, restart, drill):
        command.add_argument("--out", type=Path, help="Also write the JSON report to this file")
        command.add_argument("--label", default="", help="A short label stored in the report, such as before")
    args = parser.parse_args(argv)
    work = Path(tempfile.mkdtemp(prefix="sentinel-claims-"))
    stamp = datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        if args.command == "rate":
            sizes = [int(value) for value in args.backlog.split(",")]
            if not all(2 <= size <= 5000 for size in sizes) or not 1 <= args.clients <= 32:
                parser.error("Backlog sizes must be 2 to 5000 and clients 1 to 32")
            runs = []
            for index, size in enumerate(sizes):
                runs.append(measure_rate(work / f"rate-{index}", backlog=size, clients=args.clients))
                print(json.dumps(runs[-1]), flush=True)
            report = {"schema": "sentinel-claims-rate-v1", "label": args.label, "started_at": stamp,
                      **_labels("synthetic commands claimed over HTTP; never processed"), "runs": runs}
        elif args.command == "restore":
            if not 1 <= args.repeat <= 20 or not 0 < args.poll_seconds <= 60:
                parser.error("--repeat 1-20 and --poll-seconds up to 60")
            runs = []
            for index in range(args.repeat):
                runs.append(restore_drill(work / f"restore-{index}", poll_seconds=args.poll_seconds,
                                          staged=not args.all_at_once))
                print(json.dumps({"run": index + 1, **runs[-1]}), flush=True)
            report = {"schema": "sentinel-restore-drill-v1", "label": args.label, "started_at": stamp,
                      **_labels("restore drill: backup at READY, restored after the action ended"),
                      "staged": not args.all_at_once, "runs": runs}
        else:
            if not 1 <= args.repeat <= 20 or not 0 < args.poll_seconds <= 60 or not 0 <= args.down_seconds <= 120:
                parser.error("--repeat 1-20, --poll-seconds up to 60 and --down-seconds up to 120")
            runs = []
            for index in range(args.repeat):
                runs.append(measure_restart(work / f"restart-{index}", poll_seconds=args.poll_seconds,
                                            down_seconds=args.down_seconds))
                print(json.dumps({"run": index + 1, **runs[-1]}), flush=True)
            report = {"schema": "sentinel-claims-restart-v1", "label": args.label, "started_at": stamp,
                      **_labels("scenario S5: time from /readyz to each worker's first successful claim"),
                      "poll_seconds": args.poll_seconds, "down_seconds": args.down_seconds,
                      "summary": summarise(runs), "runs": runs}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    head = {key: report[key] for key in ("schema", "label", "backoff")}
    if args.command == "rate":
        head["claims_per_second"] = [run["claims_per_second"] for run in report["runs"]]
    elif args.command == "restart":
        head["summary"] = report["summary"]
    else:
        head["recovered"] = [run["second_end"]["states"] for run in report["runs"]]
    print(json.dumps(head), flush=True)
    if args.command == "restore":
        return 0 if all(run["second_end"]["terminal"] and run["template_writes"] == 1
                        and None not in run["store_changed_seconds"].values() for run in report["runs"]) else 1
    return 0 if all(not run.get("errors") and not run.get("harness_error") for run in report["runs"]) else 1


if __name__ == "__main__":
    sys.exit(main())
