"""Bounded local security evidence, separate from recovery authorization."""

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def import_inspections(path, events):
    """Deduplicate metadata-only inspector logs across polling/restarts."""
    if not isinstance(events, list) or len(events) > 1000:
        raise ValueError("Inspection import must be a bounded batch")
    allowed = {"kind", "at", "method", "bytes", "verdict", "rule", "items"}
    from services.security.grpc_inspection import METHODS
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS security_imports (digest TEXT PRIMARY KEY)")
            for event in events:
                if (not isinstance(event, dict) or event.keys() - allowed or event.get("kind") != "inspection"
                        or event.get("verdict") not in {"ALLOW", "DENY"}
                        or event.get("method") not in METHODS | {"unrecognized"}
                        or type(event.get("bytes")) is not int or not 0 <= event["bytes"] <= 65537
                        or "items" in event and (type(event["items"]) is not int or not 0 <= event["items"] <= 20)
                        or any(not isinstance(event.get(key), str) or len(event[key]) > 256 for key in ("at", "method", "rule"))):
                    raise ValueError("Expected projected inspection metadata")
                stamp = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
                if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
                    raise ValueError("Inspection timestamp must be UTC")
                encoded = json.dumps(event, sort_keys=True, allow_nan=False)
                digest = hashlib.sha256(encoded.encode()).hexdigest()
                inserted = db.execute("INSERT OR IGNORE INTO security_imports(digest) VALUES(?)", (digest,)).rowcount
                if inserted:
                    db.execute("INSERT INTO security_events(kind,payload) VALUES('inspection',?)", (encoded,))
            db.execute("DELETE FROM security_events WHERE kind='inspection' AND id NOT IN "
                       "(SELECT id FROM security_events WHERE kind='inspection' ORDER BY id DESC LIMIT 1000)")
            db.execute("DELETE FROM security_imports WHERE rowid NOT IN (SELECT rowid FROM security_imports ORDER BY rowid DESC LIMIT 10000)")


def record(path, kind, payload):
    if kind not in {"inspection", "image_scan", "monitor"}:
        raise ValueError("Unsupported security record")
    event = {"kind": kind, "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), **payload}
    encoded = json.dumps(event, allow_nan=False)
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
                       (kind, kind, 1000 if kind == "inspection" else 1 if kind == "monitor" else 50))
    return event


def update_alert(path, *, key, code, severity, detail, active, image=None, change_token=""):
    """Persist state transitions, suppress duplicate open alerts across restarts."""
    if code not in {"CVE_HIGH_CRITICAL", "CVE_SCAN_FAILED", "CVE_UNRATED", "INSPECTION_DENIED", "SCAN_STALE"}:
        raise ValueError("Unknown alert code")
    if severity not in {"INFO", "WARNING", "HIGH", "CRITICAL"} or type(active) is not bool:
        raise ValueError("Invalid alert state")
    event = {"kind": "alert", "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
             "code": code, "severity": severity if active else "INFO", "detail": detail,
             "status": "OPEN" if active else "RESOLVED", "image": image}
    encoded = json.dumps(event, allow_nan=False)
    if len(encoded.encode()) > 8192:
        raise ValueError("Alert exceeds evidence limit")
    fingerprint = hashlib.sha256(json.dumps([code, severity, detail, active, image, change_token]).encode()).hexdigest()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=5)) as db:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS security_events (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS security_alert_state (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, active INTEGER NOT NULL)")
            previous = db.execute("SELECT fingerprint,active FROM security_alert_state WHERE key=?", (key,)).fetchone()
            if previous and previous[0] == fingerprint or not active and (not previous or not previous[1]):
                return False
            db.execute("INSERT INTO security_alert_state(key,fingerprint,active) VALUES(?,?,?) "
                       "ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,active=excluded.active", (key, fingerprint, active))
            db.execute("INSERT INTO security_events(kind,payload) VALUES('alert',?)", (encoded,))
            db.execute("DELETE FROM security_events WHERE kind='alert' AND id NOT IN "
                       "(SELECT id FROM security_events WHERE kind='alert' ORDER BY id DESC LIMIT 500)")
    return True


def recent(db):
    """Read a bounded window from the dashboard's existing read transaction."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='security_events'").fetchone():
        return []
    return [{"sequence": row[0], **json.loads(row[1])} for row in db.execute(
        "SELECT id,payload FROM security_events WHERE id IN "
        "(SELECT id FROM security_events WHERE kind='inspection' ORDER BY id DESC LIMIT 60) "
        "OR id IN (SELECT id FROM security_events WHERE kind='image_scan' ORDER BY id DESC LIMIT 40) "
        "OR id IN (SELECT id FROM security_events WHERE kind='alert' ORDER BY id DESC LIMIT 40) "
        "OR id IN (SELECT id FROM security_events WHERE kind='monitor' ORDER BY id DESC LIMIT 1) "
        "ORDER BY id DESC")]
