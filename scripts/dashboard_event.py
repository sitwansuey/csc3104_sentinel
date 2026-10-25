"""Record a fault marker or component error; this command never injects a fault."""

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.telemetry import emit_event


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--telemetry", default="runs/telemetry.sqlite3")
    parser.add_argument("--member", type=int, choices=range(1, 7), required=True)
    parser.add_argument("--type", choices=("fault", "error", "status", "heartbeat"), required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--incident-id")
    parser.add_argument("--action-id")
    args = parser.parse_args()
    emit_event(args.telemetry, component=args.member, event_type=args.type,
               summary=args.summary, incident_id=args.incident_id, action_id=args.action_id)
    print("Observation marker recorded; no cluster action performed.")


if __name__ == "__main__":
    main()
