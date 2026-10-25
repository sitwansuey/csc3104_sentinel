"""Run Sentinel's local dashboard, building its React frontend when needed."""

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import webbrowser

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime.dashboard import make_server

FRONTEND = Path(__file__).resolve().parents[1] / "dashboard"


def ensure_frontend():
    """Build missing/changed frontend sources once, never during HTTP requests."""
    manifest = FRONTEND / "package.json"
    lockfile = FRONTEND / "package-lock.json"
    if not manifest.is_file() or not lockfile.is_file():
        raise RuntimeError("The dashboard frontend sources or package-lock.json are missing. "
                           "Restore the dashboard directory from the repository.")
    inputs = [path for path in (FRONTEND / "src").rglob("*") if path.is_file()]
    inputs.extend(path for path in FRONTEND.iterdir() if path.is_file() and (
        path.name in {"package.json", "package-lock.json", "index.html", "components.json"}
        or path.name.startswith(("vite.config.", "tsconfig", "postcss.config.", "tailwind.config."))))
    digest = hashlib.sha256()
    for path in sorted(inputs):
        digest.update(path.relative_to(FRONTEND).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    fingerprint = digest.hexdigest()
    index = FRONTEND / "dist" / "index.html"
    build_stamp = FRONTEND / "dist" / ".sentinel-build.sha256"
    if index.is_file():
        if build_stamp.is_file() and build_stamp.read_text(encoding="ascii").strip() == fingerprint:
            return
        if not build_stamp.exists() and index.stat().st_mtime_ns >= max(path.stat().st_mtime_ns for path in inputs):
            # Also recognize a fresh manual `npm run build`.
            build_stamp.write_text(fingerprint, encoding="ascii")
            return
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not node or not npm:
        raise RuntimeError("Node.js 20.19+ or 22.12+ and npm are required for the first frontend build. "
                           "Install Node.js LTS, reopen your terminal, and run this command again. "
                           "You can also build manually with: npm --prefix dashboard ci; "
                           "npm --prefix dashboard run build")
    try:
        version = subprocess.run([node, "--version"], check=True, capture_output=True,
                                 text=True, timeout=15).stdout.strip().lstrip("v")
        major, minor = (int(part) for part in version.split(".")[:2])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Cannot read the Node.js version. Install Node.js LTS and reopen your terminal.") from exc
    if not ((major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major > 22):
        raise RuntimeError(f"Node.js {version} is too old for this frontend. Install Node.js 20.19+ or 22.12+.")
    dependency_stamp = FRONTEND / "node_modules" / ".sentinel-package-lock.sha256"
    installed_lock = FRONTEND / "node_modules" / ".package-lock.json"
    lock_hash = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    if dependency_stamp.is_file():
        install = dependency_stamp.read_text(encoding="ascii").strip() != lock_hash
    else:
        install = not installed_lock.is_file() or installed_lock.stat().st_mtime_ns < lockfile.stat().st_mtime_ns
    try:
        if install:
            print("Installing dashboard frontend dependencies...", flush=True)
            subprocess.run([npm, "ci"], cwd=FRONTEND, check=True)
        dependency_stamp.write_text(lock_hash, encoding="ascii")
        print("Building the shadcn dashboard...", flush=True)
        subprocess.run([npm, "run", "build"], cwd=FRONTEND, check=True)
        if not index.is_file():
            raise RuntimeError("The frontend build completed without dashboard/dist/index.html.")
        build_stamp.write_text(fingerprint, encoding="ascii")
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("Dashboard frontend build failed. Review the npm output above. "
                           "From the repository root, run npm --prefix dashboard ci, "
                           "then npm --prefix dashboard run build, and retry.") from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("runs/coordinator.sqlite3"))
    parser.add_argument("--telemetry", type=Path, default=Path("runs/telemetry.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true", help="Open the local dashboard in your default browser")
    parser.add_argument("--demo", action="store_true", help="Isolated synthetic recovery playback; never reads your live databases")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535; use 0 to choose an available port")
    try:
        ensure_frontend()
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"dashboard: {exc}\n")
    provider = None
    cleanup = None
    if args.demo:
        from runtime.dashboard_demo import start_demo
        provider, cleanup = start_demo()
    try:
        try:
            server = make_server(args.db, args.telemetry, port=args.port, provider=provider)
        except OSError as exc:
            parser.exit(1, f"dashboard: Cannot listen on port {args.port} ({type(exc).__name__}). "
                           "Another program may be using it. Try --port 0 to choose an available port.\n")
        url = f"http://127.0.0.1:{server.server_port}"
        print(f"Sentinel dashboard: {url}", flush=True)
        print("SYNTHETIC PLAYBACK - no cluster accessed" if args.demo else
              f"Read-only | Coordinator: {args.db} | Telemetry: {args.telemetry}", flush=True)
        print("Keep this terminal open while using the dashboard. Press Ctrl+C to stop.", flush=True)
        if args.open_browser:
            def open_browser():
                try:
                    opened = webbrowser.open(url)
                except (OSError, webbrowser.Error):
                    opened = False
                if not opened:
                    print(f"Open this address manually in your browser: {url}", flush=True)
            threading.Thread(target=open_browser, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        if cleanup:
            cleanup()


if __name__ == "__main__":
    main()
