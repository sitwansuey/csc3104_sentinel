"""Read live evidence using explicit inventory/context; never change workloads."""

import argparse
import json
from pathlib import Path
import subprocess
import sys

from services.observer_kubernetes import KubectlReader
from services.verifier_http import EvidenceHTTP
from services.verifier_live import LiveEvidenceCollector, collect_window


def _read(path):
    path = Path(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Evidence input exceeds two megabytes")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--context", required=True)
    traces = parser.add_mutually_exclusive_group()
    traces.add_argument("--trace-file", help="Current OTLP JSON export, not old sample data")
    traces.add_argument("--trace-url", help="Read-only endpoint returning an OTLP JSON export, not an OTLP ingest URL")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample")
    sample.add_argument("--service", required=True)
    dependencies = commands.add_parser("dependencies")
    dependencies.add_argument("--proposal", required=True)
    window = commands.add_parser("verify")
    window.add_argument("--execution", required=True)
    window.add_argument("--policy", required=True)
    window.add_argument("--count", type=int)
    window.add_argument("--interval-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        trace_reader = None
        if args.trace_file:
            trace_reader = lambda: _read(args.trace_file)
        elif args.trace_url:
            trace_http = EvidenceHTTP(max_bytes=2_000_000)
            trace_reader = lambda: trace_http.json(args.trace_url)
        collector = LiveEvidenceCollector(_read(args.inventory), KubectlReader(args.context), trace_reader=trace_reader)
        if args.command == "sample":
            if args.service not in collector.services:
                raise ValueError("Unknown configured service")
            evidence = collector.collect(collector.services[args.service]["target"])
        elif args.command == "dependencies":
            evidence = collector.dependencies(_read(args.proposal))
        else:
            evidence = collect_window(collector, _read(args.execution), _read(args.policy),
                                      count=args.count, interval_seconds=args.interval_seconds)
        print(json.dumps({"mode": "configured_live_sources", "evidence": evidence}, allow_nan=False))
        return 0
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        print(json.dumps({"kind": "verifier_evidence_error", "message": "Check inventory, inputs and source availability"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
