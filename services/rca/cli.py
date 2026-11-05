"""Run RCA on an Observer incident and locally exported telemetry."""

import argparse
import json
from pathlib import Path

from services.rca.core import diagnose


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incident", required=True)
    parser.add_argument("--telemetry", required=True, help="Existing directory named telemetry")
    parser.add_argument("--output", required=True, help="New evidence output directory")
    parser.add_argument("--config", help="JSON overrides for data/sentinel/samples/rca-config.json")
    args = parser.parse_args()
    incident = json.loads(Path(args.incident).read_text(encoding="utf-8"))
    config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else None
    print(json.dumps(diagnose(incident, args.telemetry, args.output, config=config), indent=2))


if __name__ == "__main__":
    main()
