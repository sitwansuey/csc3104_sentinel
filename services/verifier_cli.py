"""Run one durable local Verifier pass using explicitly supplied offline files."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

from runtime.verifier_pipeline import process_verifier_requests
from services.coordinator import LocalEventBus, SQLiteStore
from services.verifier_store import VerificationStore, _utc


def _read(path):
    path = Path(path)
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Offline input exceeds 16 MiB")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coordinator-db", required=True)
    parser.add_argument("--state", required=True, help="Separate persistent Verifier SQLite file")
    parser.add_argument("--policy", required=True)
    parser.add_argument("--samples", required=True, help="JSON list for a single configured workload")
    parser.add_argument("--now", help="UTC fixture-replay time; never a live observation clock")
    args = parser.parse_args(argv)
    try:
        if not Path(args.coordinator_db).is_file():
            raise ValueError("Coordinator database must already exist")
        replay_time = _utc(args.now) if args.now else None
        clock = (lambda: replay_time) if replay_time else (lambda: datetime.now(timezone.utc))
        with closing(SQLiteStore(args.coordinator_db)) as store, closing(VerificationStore(args.state)) as ledger:
            results = process_verifier_requests(store, LocalEventBus(), ledger=ledger,
                policy_reader=lambda execution: _read(args.policy),
                sample_reader=lambda execution: _read(args.samples), clock=clock)
            pending = ledger.db.execute("SELECT count(*) FROM verification_jobs WHERE outcome IS NULL").fetchone()[0]
            print(json.dumps({"mode": "offline_file", "outcomes": results,
                              "pending_jobs": pending, "pending_outcomes": ledger.pending_outcomes()}))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"kind": "verifier_error", "message": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
