"""Run the persistent Buddy 2 Observer, or inspect its state/readiness."""

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import signal
import sqlite3
import sys
import threading

from runtime.observer_pipeline import LocalCoordinatorSink, ObserverStore, is_ready, status_file
from services.observer.kubernetes import InClusterReader, KubectlReader, KubernetesEvidenceCollector
from services.observer_telemetry import CorrelatedEvidenceCollector, TelemetryStore
from runtime.observer_fleet import fleet_health, load_targets, run_fleet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--state", type=Path, default=Path("data/sentinel/runs/observer.sqlite3"))
    run.add_argument("--coordinator-db", type=Path, default=Path("data/sentinel/runs/coordinator.sqlite3"))
    run.add_argument("--policy", type=Path, default=Path("data/sentinel/samples/policy.json"))
    source = run.add_mutually_exclusive_group(required=True)
    source.add_argument("--context")
    source.add_argument("--in-cluster", action="store_true")
    run.add_argument("--namespace")
    run.add_argument("--deployment")
    run.add_argument("--targets", type=Path, help="Explicit multi-workload target configuration")
    run.add_argument("--telemetry-db", type=Path, help="Observer-owned OTLP evidence database")
    run.add_argument("--service-name", action="append", help="OTel service.name for a single target")
    run.add_argument("--cluster-id", required=True)
    run.add_argument("--collect-logs", action="store_true")
    run.add_argument("--interval-seconds", type=float, default=15)
    run.add_argument("--poll-count", type=int, default=0, help="0 means run until SIGTERM/Ctrl+C")
    run.add_argument("--max-consecutive-errors", type=int, default=3)
    for name in ("status", "pending", "health"):
        sub = commands.add_parser(name)
        sub.add_argument("--state", type=Path, default=Path("data/sentinel/runs/observer.sqlite3"))
    fleet = commands.add_parser("fleet-health")
    fleet.add_argument("--state", type=Path, required=True)
    fleet.add_argument("--targets", type=Path, required=True)
    fleet.add_argument("--cluster-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "fleet-health":
            result = fleet_health(args.state, args.cluster_id, load_targets(args.targets))
            print(json.dumps(result, sort_keys=True))
            return int(not result["ready"])
        if args.command in ("status", "health"):
            status = status_file(args.state)
            status["ready"] = is_ready(status)
            print(json.dumps(status, sort_keys=True))
            return int(args.command == "health" and not status["ready"])
        if args.command == "pending":
            with closing(sqlite3.connect(args.state.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
                for row in connection.execute("SELECT payload FROM observer_outbox WHERE delivered=0 ORDER BY rowid"):
                    print(row[0])
            return 0
        policy = json.loads(args.policy.read_text(encoding="utf-8-sig"))
        if not isinstance(policy, dict):
            raise ValueError("Observer policy must be a JSON object")
        detection = policy.get("detection")
        if detection is None:
            detection = {}
        if not isinstance(detection, dict):
            raise ValueError("Observer detection policy must be an object")
        policy["detection"] = detection
        if args.targets:
            if args.namespace or args.deployment or args.service_name:
                parser.error("Use --targets or a single namespace/deployment, not both")
            load_targets(args.targets)
        elif not args.namespace or not args.deployment:
            parser.error("A namespace and deployment, or --targets, are required")
        if not isinstance(policy.get("telemetry", {}), dict):
            raise ValueError("Observer telemetry policy must be an object")
        if policy.get("telemetry") and not args.telemetry_db:
            parser.error("A telemetry policy requires --telemetry-db")
        if args.service_name and not args.telemetry_db:
            parser.error("--service-name requires --telemetry-db")
        gap = detection.setdefault("max_sample_gap_seconds", 45)
        if (not math.isfinite(args.interval_seconds) or args.interval_seconds <= 0
                or type(gap) is not int or gap <= args.interval_seconds
                or args.poll_count < 0 or args.max_consecutive_errors < 1):
            parser.error("Use a positive interval below the integer max_sample_gap_seconds, nonnegative poll count and positive error limit")
        if args.state.resolve() == args.coordinator_db.resolve():
            parser.error("Observer and Coordinator require separate database files")
        reader = InClusterReader() if args.in_cluster else KubectlReader(args.context)
        collector = None
        if not args.targets:
            collector = KubernetesEvidenceCollector(reader, namespace=args.namespace, name=args.deployment,
                                                     cluster_id=args.cluster_id, collect_logs=args.collect_logs)
            if args.telemetry_db:
                collector = CorrelatedEvidenceCollector(collector, TelemetryStore(args.telemetry_db),
                    args.service_name or [args.deployment], policy.get("telemetry", {}))
        sink = LocalCoordinatorSink(args.coordinator_db)
        stop = threading.Event()
        previous_handlers = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous_handlers[signum] = signal.signal(signum, lambda *_: stop.set())
        try:
            if args.targets:
                return run_fleet(args, reader, policy, sink, stop)
            scoped_policy = {**policy, "observer_service_names": args.service_name or [args.deployment]} if args.telemetry_db else policy
            with ObserverStore(args.state, scoped_policy, collector.target) as store:
                store.flush(sink)
                polls, consecutive_errors = 0, 0
                while not stop.is_set() and (args.poll_count == 0 or polls < args.poll_count):
                    try:
                        observation = collector.collect()
                        incident = store.observe(observation)
                        consecutive_errors = 0
                        event = {"kind": "observer.poll", "captured_at": observation["captured_at"],
                                 "target": collector.target, "pods_read": len(observation["pods"]),
                                 "events_read": len(observation["events"]), "logs_read": len(observation["logs"]),
                                 "warnings": observation["collection_warnings"],
                                 "incident_id": incident["incident_id"] if incident else None}
                    except Exception as error:
                        consecutive_errors += 1
                        store.collection_failed(error)
                        event = {"kind": "observer.collection_error", "error": type(error).__name__,
                                 "consecutive_errors": consecutive_errors}
                    delivery = store.flush(sink)
                    print(json.dumps({**event, "delivery": delivery, "status": store.status()}, sort_keys=True), flush=True)
                    polls += 1
                    if consecutive_errors >= args.max_consecutive_errors:
                        return 1
                    if not stop.is_set() and (args.poll_count == 0 or polls < args.poll_count):
                        stop.wait(args.interval_seconds)
                return int(bool(consecutive_errors or store.status()["last_delivery_error"]))
        finally:
            for signum, previous in previous_handlers.items():
                signal.signal(signum, previous)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(f"observer: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
