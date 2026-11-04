"""Bounded local security evidence, separate from recovery authorization."""

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def record(path, kind, payload):
    if kind not in {"inspection", "image_scan"}:
        raise ValueError("Unsupported security record")
    encoded = json.dumps({"kind": kind, "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                          **payload}, allow_nan=False)
    if len(encoded.encode()) > 1024 * 1024:
        raise ValueError("Security record exceeds one MiB")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("INSERT INTO security_events(kind,payload) VALUES(?,?)", (kind, encoded))
            db.execute("DELETE FROM security_events WHERE kind=? AND id NOT IN "
                       "(SELECT id FROM security_events WHERE kind=? ORDER BY id DESC LIMIT ?)",
                       (kind, kind, 1000 if kind == "inspection" else 50))


def recent(db):
    """Read a bounded window from the dashboard's existing read transaction."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='security_events'").fetchone():
        return []
    return [{"sequence": row[0], **json.loads(row[1])} for row in db.execute(
        "SELECT id,payload FROM security_events WHERE id IN "
        "(SELECT id FROM security_events WHERE kind='inspection' ORDER BY id DESC LIMIT 60) "
        "OR id IN (SELECT id FROM security_events WHERE kind='image_scan' ORDER BY id DESC LIMIT 40) "
        "ORDER BY id DESC")]
