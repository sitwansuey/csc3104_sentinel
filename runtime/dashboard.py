"""Read-only local dashboard API. No approvals, ingestion or Kubernetes writes."""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "dashboard" / "dist"
LIMIT = 600
CLUSTER_HISTORY_LIMIT = 60


def _frontend_file(relative):
    """Resolve only built public files, without following a link outside dist."""
    root = ASSETS.resolve()
    candidate = (root / relative).resolve(strict=True)
    candidate.relative_to(root)
    if not candidate.is_file():
        raise FileNotFoundError(relative)
    return candidate


def _open_readonly(path):
    # mode=ro prevents accidentally creating a misspelled database or migrating it.
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
    except BaseException:
        db.close()
        raise
    return db


def _decode(row, fields):
    item = dict(row)
    for field in fields:
        item[field] = json.loads(item[field]) if item[field] else None
    return item


def snapshot(db_path, telemetry_path):
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "recorded/live producers", "limit": LIMIT,
        "sources": {}, "incidents": [], "actions": [], "messages": [],
        "approvals": [], "outbox": [], "reports": [], "telemetry": [], "cluster_history": [],
    }
    try:
        db = _open_readonly(db_path)
        try:
            result["incidents"] = [_decode(row, ("target",)) for row in db.execute(
                "SELECT rowid AS sequence,* FROM incidents ORDER BY rowid DESC LIMIT ?", (LIMIT,))]
            result["actions"] = [_decode(row, ("proposal", "guard_result", "execution_result")) for row in db.execute(
                "SELECT rowid AS sequence,* FROM actions ORDER BY rowid DESC LIMIT ?", (LIMIT,))]
            result["messages"] = [{"sequence": row[0], "message": json.loads(row[1])} for row in db.execute(
                "SELECT rowid,payload FROM messages ORDER BY rowid DESC LIMIT ?", (LIMIT,))]
            result["approvals"] = [{"id": row[0], "record": json.loads(row[1])} for row in db.execute(
                "SELECT id,record FROM approvals ORDER BY id DESC LIMIT ?", (LIMIT,))]
            result["outbox"] = [dict(row) for row in db.execute(
                "SELECT id,topic,action_id,delivered FROM outbox ORDER BY id DESC LIMIT ?", (LIMIT,))]
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='guard_reports'").fetchone():
                result["reports"] = [json.loads(row[0]) for row in db.execute(
                    "SELECT report FROM guard_reports ORDER BY rowid DESC LIMIT ?", (LIMIT,))]
            result["sources"]["coordinator"] = {"connected": True}
        finally:
            db.close()
    except (sqlite3.Error, ValueError, OSError):
        for field in ("incidents", "actions", "messages", "approvals", "outbox", "reports"):
            result[field] = []
        result["sources"]["coordinator"] = {
            "connected": False, "detail": "Coordinator database unavailable, busy or incompatible. Retrying."}
    try:
        db = _open_readonly(telemetry_path)
        try:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='telemetry'").fetchone():
                result["telemetry"] = [{"sequence": row[0], **json.loads(row[1])} for row in db.execute(
                    "SELECT id,payload FROM telemetry ORDER BY id DESC LIMIT ?", (LIMIT,))]
                result["sources"]["telemetry"] = {"connected": True}
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='cluster_snapshots'").fetchone():
                result["cluster_history"] = [{"sequence": row[0], **json.loads(row[1])} for row in db.execute(
                    "SELECT id,payload FROM cluster_snapshots ORDER BY id DESC LIMIT ?", (CLUSTER_HISTORY_LIMIT,))]
                result["sources"]["cluster_metrics"] = {"connected": bool(result["cluster_history"])}
        finally:
            db.close()
    except (sqlite3.Error, ValueError, OSError):
        result["telemetry"], result["cluster_history"] = [], []
        result["sources"].pop("telemetry", None)
        result["sources"].pop("cluster_metrics", None)
    result["sources"].setdefault("telemetry", {
        "connected": False, "detail": "No telemetry journal connected. Start the collector or emit an event."})
    result["sources"].setdefault("cluster_metrics", {
        "connected": False, "detail": "No cluster snapshots recorded. Start the Kubernetes collector."})
    return result


def make_server(db_path, telemetry_path, *, port=8765, provider=None, bind="127.0.0.1"):
    if bind not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("Dashboard bind must be loopback or explicit container bind")
    read_snapshot = provider or (lambda: snapshot(db_path, telemetry_path))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                route = urlsplit(self.path).path
            except ValueError:
                self.send_error(404)
                return
            if route == "/api/snapshot":
                try:
                    body = json.dumps(read_snapshot(), allow_nan=False).encode("utf-8")
                except Exception:
                    self.send_error(503, "Snapshot unavailable")
                    return
                content_type = "application/json; charset=utf-8"
            elif route in {"/", "/index.html"}:
                try:
                    body = _frontend_file("index.html").read_bytes()
                except (OSError, ValueError, RuntimeError):
                    self._respond(503, (
                        "The dashboard frontend has not been built. From the repository root, run:\n\n"
                        "npm --prefix dashboard ci\n"
                        "npm --prefix dashboard run build\n\n"
                        "Then restart python scripts/dashboard.py --demo.\n"
                    ).encode("utf-8"), "text/plain; charset=utf-8")
                    return
                content_type = "text/html; charset=utf-8"
            elif route.startswith("/assets/"):
                # Vite uses plain hashed asset names. Reject encoded separators,
                # traversal and Windows path separators before filesystem access.
                if "%" in route or "\\" in route or any(
                        part in {"", ".", ".."} for part in route[1:].split("/")):
                    self.send_error(404)
                    return
                try:
                    asset = _frontend_file(route[1:])
                    body = asset.read_bytes()
                except (OSError, ValueError, RuntimeError):
                    self.send_error(404)
                    return
                content_type = {
                    ".js": "text/javascript; charset=utf-8",
                    ".css": "text/css; charset=utf-8",
                    ".svg": "image/svg+xml",
                }.get(asset.suffix, mimetypes.guess_type(asset.name)[0] or "application/octet-stream")
            else:
                self.send_error(404)
                return
            self._respond(200, body, content_type)

        def _respond(self, status, body, content_type):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    # Host launches default to loopback. Container launchers explicitly opt in to
    # all container interfaces and publish only on host loopback.
    return ThreadingHTTPServer((bind, port), Handler)
