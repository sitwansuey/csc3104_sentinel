"""Small, optional observation journal. Never an input to Guard authorization.

Each process opens its own short-lived SQLite connection. Keep this journal on
one host; it is not a cross-host broker or a store for raw logs/Secret values.
"""

from datetime import datetime, timezone
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

EVENT_TYPES = {"observation", "error", "heartbeat", "fault", "status"}
CLUSTER_SNAPSHOT_LIMIT = 2 * 1024 * 1024


def emit_event(path, *, component, event_type, summary, target=None, metrics=None,
               incident_id=None, action_id=None, at=None, progress=None):
    if type(component) is not int or component not in range(1, 7):
        raise ValueError("component must be a member number from 1 to 6")
    if event_type not in EVENT_TYPES or not isinstance(summary, str) or not summary.strip():
        raise ValueError("A supported event_type and summary are required")
    timestamp = at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("at must be a UTC timestamp")
    if target is not None:
        required = {"cluster_id", "namespace", "kind", "name", "uid"}
        if not isinstance(target, dict) or target.keys() != required or any(
                not isinstance(value, str) or not value for value in target.values()):
            raise ValueError("target must contain the exact five target identity strings")
    if metrics is not None and (not isinstance(metrics, dict) or any(
            key not in {"desired", "ready", "available", "generation", "observed_generation"}
            or type(value) is not int or value < 0 for key, value in metrics.items())):
        raise ValueError("Only nonnegative replica and generation metrics are accepted")
    if progress is not None:
        allowed = {"stage", "status", "role", "model", "seconds", "attempt", "call_id", "tool",
                   "round", "evidence_id", "tool_calls", "cache_hit", "prompt_characters",
                   "prompt_tokens", "output_tokens", "error_type", "load_duration_seconds",
                   "prompt_eval_duration_seconds", "eval_duration_seconds"}
        if (not isinstance(progress, dict) or not {"stage", "status"} <= progress.keys()
                or not progress.keys() <= allowed or any(
                    not ((isinstance(v, str) and len(v) <= 200) or type(v) is bool or
                         (type(v) in (int, float) and math.isfinite(v) and v >= 0)) for v in progress.values())):
            raise ValueError("Only bounded operational progress metadata is accepted")
    payload = dict(component=component, event_type=event_type, summary=summary,
                   target=target, metrics=metrics or {}, incident_id=incident_id, action_id=action_id,
                   at=timestamp)
    if progress is not None:
        payload["progress"] = progress
    encoded = json.dumps(payload, allow_nan=False)
    if len(encoded.encode("utf-8")) > 16384:
        raise ValueError("Telemetry event exceeds 16 KiB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS telemetry (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT INTO telemetry(payload) VALUES(?)", (encoded,))
    return payload


def emit_cluster_snapshot(path, snapshot):
    """Store projected cluster data separately from the 16 KiB event journal.

    The collector owns projection/normalization. This transport rejects unexpected
    top-level fields and never stores a raw Kubernetes object as a snapshot.
    """
    required = {"schema_version", "cluster_id", "namespace", "observed_at", "sources",
                "nodes", "pods", "events", "summary", "truncation"}
    if not isinstance(snapshot, dict) or snapshot.keys() != required or snapshot.get("schema_version") != "1.0":
        raise ValueError("A projected cluster snapshot is required")
    for key in ("nodes", "pods", "events"):
        if not isinstance(snapshot[key], list):
            raise ValueError("Cluster resource lists are required")
    timestamp = snapshot["observed_at"]
    parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError("observed_at must be a UTC timestamp")
    encoded = json.dumps(snapshot, allow_nan=False)
    if len(encoded.encode("utf-8")) > CLUSTER_SNAPSHOT_LIMIT:
        raise ValueError("Cluster snapshot exceeds 2 MiB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS cluster_snapshots (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
            db.execute("INSERT INTO cluster_snapshots(payload) VALUES(?)", (encoded,))
            db.execute("DELETE FROM cluster_snapshots WHERE id NOT IN (SELECT id FROM cluster_snapshots ORDER BY id DESC LIMIT 600)")
    return snapshot
