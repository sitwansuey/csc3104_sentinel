"""Read-only dashboard, with optional gates for a dedicated lab harness.

The default API has no mutation routes. Opt-in lab controls only release a fixed
scenario owned by the harness; they cannot approve or execute recovery actions.
"""

from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from services.coordinator_read import read_records

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


def snapshot(db_path, telemetry_path):
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "mode": "recorded/live producers", "limit": LIMIT,
        "sources": {}, "incidents": [], "actions": [], "messages": [],
        "approvals": [], "outbox": [], "reports": [], "telemetry": [], "cluster_history": [],
    }
    try:
        result.update(read_records(db_path, limit=LIMIT))
        result["sources"]["coordinator"] = {"connected": True}
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
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='boutique_observations'").fetchone():
                result["boutique_history"] = [{"sequence": row[0], **json.loads(row[1])} for row in db.execute(
                    "SELECT id,payload FROM boutique_observations ORDER BY id DESC LIMIT 180")]
        finally:
            db.close()
    except (sqlite3.Error, ValueError, OSError):
        result["telemetry"], result["cluster_history"] = [], []
        result["sources"].pop("telemetry", None)
        result["sources"].pop("cluster_metrics", None)
        result.pop("boutique_history", None)
    result["sources"].setdefault("telemetry", {
        "connected": False, "detail": "No telemetry journal connected. Start the collector or emit an event."})
    result["sources"].setdefault("cluster_metrics", {
        "connected": False, "detail": "No cluster snapshots recorded. Start the Kubernetes collector."})
    return result


def make_server(db_path, telemetry_path, *, port=8765, provider=None, bind="127.0.0.1", lab_control=None):
    if bind not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("Dashboard bind must be loopback or explicit container bind")
    read_snapshot = provider or (lambda: snapshot(db_path, telemetry_path))

    class Handler(BaseHTTPRequestHandler):
        def _local_host(self):
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1:
                return None
            try:
                parsed = urlsplit("http://" + hosts[0])
                valid = (parsed.hostname in {"127.0.0.1", "localhost"}
                         and parsed.username is None and parsed.password is None
                         and not parsed.path and not parsed.query and not parsed.fragment
                         and parsed.port is not None and 1 <= parsed.port <= 65535)
                return hosts[0] if valid else None
            except ValueError:
                return None

        def do_GET(self):
            try:
                route = urlsplit(self.path).path
            except ValueError:
                self.send_error(404)
                return
            if route == "/api/snapshot":
                try:
                    result = read_snapshot()
                    if lab_control is not None:
                        if not self._local_host():
                            self.send_error(403)
                            return
                        result = {**result, "lab": lab_control.snapshot()}
                    body = json.dumps(result, allow_nan=False).encode("utf-8")
                except Exception:
                    self.send_error(503, "Snapshot unavailable")
                    return
                content_type = "application/json; charset=utf-8"
            elif route == "/api/lab" and lab_control is not None:
                if not self._local_host():
                    self.send_error(403)
                    return
                body = json.dumps(lab_control.capabilities()).encode("utf-8")
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
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Closing/refreshing the browser does not undo an accepted gate.
                # A later click replays that gate instead of releasing another.
                pass

        def log_message(self, *args):
            pass

    # Host launches default to loopback. Container launchers explicitly opt in to
    # all container interfaces and publish only on host loopback.
    class LabHandler(Handler):
        def do_POST(self):
            from runtime.lab_control import LabConflict
            if self.path != "/api/lab/inject":
                self.send_error(404)
                return
            host = self._local_host()
            if (not host or self.headers.get("Origin") != "http://" + host
                    or not lab_control.authenticates(self.headers.get("X-Sentinel-Lab-Token"))):
                self.send_error(403)
                return
            if self.headers.get_content_type() != "application/json" or self.headers.get("Transfer-Encoding"):
                self.send_error(415)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048:
                    raise ValueError("Invalid body length")
                self.connection.settimeout(3)
                payload = json.loads(self.rfile.read(size))
                if not isinstance(payload, dict) or set(payload) != {"session_id", "gate_id"}:
                    raise ValueError("Invalid request")
                result = lab_control.request_injection(**payload)
            except LabConflict:
                self._respond(409, b'{"error":"Scenario changed; refresh before injecting."}', "application/json")
                return
            except (ValueError, TypeError, TimeoutError):
                self.send_error(400)
                return
            except OSError:
                self.send_error(503, "Lab control unavailable")
                return
            self._respond(202, json.dumps(result).encode("utf-8"), "application/json")

    return ThreadingHTTPServer((bind, port), Handler if lab_control is None else LabHandler)
