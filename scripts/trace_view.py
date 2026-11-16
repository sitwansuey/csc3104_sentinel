"""Print a waterfall per trace from Sentinel JSON logs, offline.

Reads span.end lines (and coordinator.state.transition lines) from one or more
JSON log files, as written with SENTINEL_LOG_FORMAT=json, and rebuilds each
trace as a tree. Lines that are not JSON log records are skipped. With --db, the
Coordinator's transitions journal for each traced incident is listed too; the
database is opened read only and never migrated.

Offsets come from each line's wall-clock ts, so spans from processes on
different hosts line up only as well as their clocks agree.

Example:
  python scripts/trace_view.py coordinator.log planner.log --db data/coordinator.db
"""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.tracing import parse_traceparent  # noqa: E402

DETAILS = ("topic", "operation", "message_kind", "http_status", "outcome", "error_type")


def _time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def read_lines(lines):
    """JSON log records from text lines; anything else is skipped."""
    records = []
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and isinstance(record.get("event"), str) and isinstance(record.get("ts"), str):
            records.append(record)
    return records


def build(records):
    """Group span.end records into traces: {trace_id: {"spans": {...}, "transitions": [...]}}."""
    traces = {}
    for record in records:
        trace_id = record.get("trace_id")
        if not isinstance(trace_id, str):
            continue
        if record["event"] == "span.end" and isinstance(record.get("span_id"), str):
            try:
                end = _time(record["ts"])
            except ValueError:
                continue
            duration = record.get("duration_ms") if isinstance(record.get("duration_ms"), (int, float)) else 0
            trace = traces.setdefault(trace_id, {"spans": {}, "transitions": []})
            trace["spans"][record["span_id"]] = dict(record, start=end - duration / 1000, end=end,
                                                     duration_ms=duration, children=[])
        elif record["event"] == "coordinator.state.transition":
            traces.setdefault(trace_id, {"spans": {}, "transitions": []})["transitions"].append(record)
    for trace in traces.values():
        spans = trace["spans"]
        trace["roots"] = []
        for span in sorted(spans.values(), key=lambda item: item["start"]):
            parent = spans.get(span.get("parent_span_id"))
            if parent is not None and parent is not span:
                parent["children"].append(span)
            else:
                span["missing_parent"] = span.get("parent_span_id") is not None
                trace["roots"].append(span)
    return traces


def journal(database):
    """Transitions per trace_id from a Coordinator database, opened read only."""
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(incidents)")}
        has_journal = db.execute("SELECT 1 FROM sqlite_master WHERE name='transitions'").fetchone()
        if "trace_context" not in columns or not has_journal:
            return {}
        result = {}
        for incident_id, traceparent in db.execute("SELECT incident_id,trace_context FROM incidents"):
            context = parse_traceparent(traceparent)
            if context is None:
                continue
            rows = db.execute("""SELECT seq,action_id,from_state,to_state,cause,recorded_at FROM transitions
                WHERE incident_id=? ORDER BY seq""", (incident_id,)).fetchall()
            result.setdefault(context.trace_id, []).append(
                {"incident_id": incident_id, "transitions": [dict(zip(
                    ("seq", "action_id", "from_state", "to_state", "cause", "recorded_at"), row, strict=True))
                    for row in rows]})
        return result


def _bar(span, origin, total, width):
    if width <= 0:
        return ""
    scale = width / total if total > 0 else 0
    left = min(width - 1, int((span["start"] - origin) * 1000 * scale))
    length = max(1, min(width - left, round(span["duration_ms"] * scale)))
    return "|" + " " * left + "#" * length + " " * (width - left - length) + "| "


def render(trace_id, trace, *, width=40, journal_rows=()):
    spans = trace["spans"]
    lines = []
    if spans:
        origin = min(span["start"] for span in spans.values())
        total = (max(span["end"] for span in spans.values()) - origin) * 1000
    else:
        origin, total = 0, 0
    incidents = sorted({span["incident_id"] for span in spans.values() if isinstance(span.get("incident_id"), str)})
    lines.append(f"trace {trace_id}  spans={len(spans)}  total={total:.1f} ms"
                 + (f"  incident={','.join(incidents)}" if incidents else ""))
    by_span = {}
    for item in trace["transitions"]:
        by_span.setdefault(item.get("span_id"), []).append(item)

    def visit(span, depth):
        details = " ".join(f"{key}={span[key]}" for key in DETAILS if key in span)
        status = "" if span.get("status") == "ok" else f" status={span.get('status')}"
        note = "  (parent not in these logs)" if span.get("missing_parent") else ""
        lines.append(f"{(span['start'] - origin) * 1000:>9.1f} {span['duration_ms']:>9.1f} ms "
                     f"{_bar(span, origin, total, width)}{'  ' * depth}{span.get('span_name', '?')} "
                     f"[{span.get('component', '?')}]{status} {details}{note}".rstrip())
        for item in sorted(by_span.pop(span["span_id"], []), key=lambda r: r.get("seq", 0)):
            lines.append(f"{'':>23}{' ' * (width + 3 if width > 0 else 0)}{'  ' * (depth + 1)}"
                         f"> {item.get('from_state') or '-'} -> {item.get('to_state')} (seq {item.get('seq')})")
        for child in span["children"]:
            visit(child, depth + 1)

    for root in trace.get("roots", []):
        visit(root, 0)
    for items in by_span.values():
        for item in items:
            lines.append(f"  transition outside logged spans: {item.get('from_state') or '-'} -> "
                         f"{item.get('to_state')} (seq {item.get('seq')})")
    for entry in journal_rows:
        lines.append(f"  journal for {entry['incident_id']}:")
        for row in entry["transitions"]:
            lines.append(f"    seq {row['seq']:>4}  {row['recorded_at']}  {row['from_state'] or '-'} -> "
                         f"{row['to_state']}  cause={row['cause']}")
    return lines


def main(argv=None, stdout=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("logs", nargs="+", help="JSON log files; '-' reads standard input")
    parser.add_argument("--db", help="Coordinator database whose transitions journal to list per trace")
    parser.add_argument("--trace", help="Show only traces whose id starts with this prefix")
    parser.add_argument("--width", type=int, default=40, help="Bar width in characters; 0 hides the bars")
    args = parser.parse_args(argv)
    stdout = stdout or sys.stdout
    if args.width < 0:
        parser.error("--width must be zero or more")
    records = []
    for name in args.logs:
        if name == "-":
            records += read_lines(sys.stdin)
            continue
        try:
            with open(name, encoding="utf-8", errors="replace") as handle:
                records += read_lines(handle)
        except OSError as error:
            parser.error(f"cannot read {name}: {type(error).__name__}")
    rows = {}
    if args.db:
        try:
            rows = journal(args.db)
        except sqlite3.Error as error:
            parser.error(f"cannot read {args.db}: {type(error).__name__}")
    traces = {key: value for key, value in build(records).items()
              if value["spans"] and (not args.trace or key.startswith(args.trace))}
    if not traces:
        print("No span.end records found.", file=stdout)
        return 1
    ordered = sorted(traces.items(), key=lambda item: min(span["start"] for span in item[1]["spans"].values()))
    for index, (trace_id, trace) in enumerate(ordered):
        if index:
            print(file=stdout)
        for line in render(trace_id, trace, width=args.width, journal_rows=rows.get(trace_id, ())):
            print(line, file=stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
