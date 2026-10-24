"""Local coordinator CLI; prints pending routing envelopes for local adapters."""

import argparse
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.coordinator import (
    CoordinatorError, LocalEventBus, SQLiteStore, acknowledge, approve,
    dispatch_pending, handle,
)
from runtime.guard_pipeline import KubectlStateReader, json_reader, process_guard_requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="runs/coordinator.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="Record one wire-message JSON file")
    ingest.add_argument("file", type=Path)
    approval = commands.add_parser("approve", help="Record operator approval; queue Guard revalidation")
    approval.add_argument("action_id")
    approval.add_argument("operator_id")
    approval.add_argument("--expires-at", help="Future UTC expiry; required by strict live Guard policies")
    status = commands.add_parser("status")
    status.add_argument("incident_id")
    commands.add_parser("pending", help="Replay unacknowledged routing envelopes")
    guard = commands.add_parser("guard", help="Process pending requests using the real Recovery Guard")
    guard.add_argument("--policy", required=True, type=Path)
    guard.add_argument("--baseline", type=Path)
    guard.add_argument("--dependencies", type=Path, help="Otherwise use stored dependency results or UNKNOWN")
    state_source = guard.add_mutually_exclusive_group(required=True)
    state_source.add_argument("--state", type=Path, help="Recorded observation JSON (offline mode)")
    state_source.add_argument("--context", help="Read current Deployment via this kubectl context")
    guard.add_argument("--cluster-id", help="Required with --context")
    guard.add_argument("--now", help="Fixed UTC clock for offline fixtures only")
    ack = commands.add_parser("ack", help="Acknowledge an envelope after consumer success")
    ack.add_argument("event_id", type=int)
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    try:
        with SQLiteStore(args.db) as store:
            bus = LocalEventBus()
            if args.command == "ingest":
                handle(json.loads(args.file.read_text(encoding="utf-8")), store, bus)
            elif args.command == "approve":
                print(json.dumps({"approval": approve(args.action_id, args.operator_id, store,
                                                       expires_at=args.expires_at)}))
            elif args.command == "status":
                incident = store.incident(args.incident_id)
                if incident is None:
                    raise CoordinatorError("Unknown incident")
                print(json.dumps(incident))
                return
            elif args.command == "ack":
                acknowledge(store, args.event_id)
                return
            elif args.command == "guard":
                if args.context and (not args.cluster_id or args.now):
                    raise ValueError("Live reads require --cluster-id and do not allow --now")
                clock = None
                if args.now:
                    now = datetime.fromisoformat(args.now.replace("Z", "+00:00"))
                    if now.utcoffset() != timedelta(0):
                        raise ValueError("--now requires a timezone-aware UTC timestamp")
                    clock = lambda: now
                state_reader = json_reader(args.state) if args.state else KubectlStateReader(args.context, args.cluster_id)
                results = process_guard_requests(
                    store, bus, state_reader=state_reader, policy_reader=json_reader(args.policy),
                    baseline_reader=json_reader(args.baseline) if args.baseline else None,
                    dependency_reader=json_reader(args.dependencies) if args.dependencies else None,
                    clock=clock,
                )
                for result in results:
                    print(json.dumps({"guard_result": result}))
            dispatch_pending(store, bus)
            for event in bus.events:
                print(json.dumps(event))
    except (ValueError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        parser.exit(1, f"coordinator: {exc}\n")


if __name__ == "__main__":
    main()
