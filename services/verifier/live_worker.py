"""Poll read-only live evidence and checkpoint each pass in a durable Verifier DB."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import time

from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator.core import LocalEventBus, SQLiteStore
from services.observer.kubernetes import KubectlReader
from services.verifier.cli import _read
from services.verifier.http import EvidenceHTTP
from services.verifier.live import LiveEvidenceCollector
from services.verifier.live_cli import _read as _read_trace
from services.verifier.store import VerificationStore


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-db", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--context", required=True)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--trace-url")
    source.add_argument("--trace-file")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5)
    args = parser.parse_args(argv)
    try:
        if not 0.1 <= args.poll_seconds <= 60:
            raise ValueError("Polling interval must be between 0.1 and 60 seconds")
        if not Path(args.coordinator_db).is_file():
            raise ValueError("Coordinator database must already exist")
        traces = None
        if args.trace_url:
            http = EvidenceHTTP(max_bytes=2_000_000)
            traces = lambda: http.json(args.trace_url)
        elif args.trace_file:
            traces = lambda: _read_trace(args.trace_file)
        collector = LiveEvidenceCollector(_read(args.inventory), KubectlReader(args.context), trace_reader=traces)
        with closing(SQLiteStore(args.coordinator_db)) as store, closing(VerificationStore(args.state)) as ledger:
            while True:
                outcomes = process_verifier_requests(store, LocalEventBus(), ledger=ledger,
                    policy_reader=lambda execution: _read(args.policy), sample_reader=collector.samples)
                jobs = [{"action_id": row["action_id"], "sample_count": len(json.loads(row["samples"])),
                         "started_at": row["started_at"], "deadline": row["deadline"],
                         "finished": row["outcome"] is not None, "delivered": bool(row["delivered"])}
                        for row in ledger.db.execute("SELECT * FROM verification_jobs ORDER BY action_id")]
                print(json.dumps({"mode": "durable_live", "outcomes": outcomes, "jobs": jobs}, allow_nan=False), flush=True)
                pending = store.db.execute("SELECT count(*) FROM outbox WHERE topic='verifier.request' AND delivered=0").fetchone()[0]
                if not args.watch or not pending:
                    return 0
                time.sleep(args.poll_seconds)
    except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
        print(json.dumps({"kind": "verifier_live_error", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
