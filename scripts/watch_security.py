"""Scheduled local CVE scans and dashboard alerts. No automatic remediation."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.security_store import record, update_alert
from scripts.scan_image_security import run_scan
from services.executor.store import LocalTargetCoordinator, TargetBusy
from services.image_security import image_reference


def stamp():
    return datetime.now(timezone.utc)


def load_inventory(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, list) or not 1 <= len(data) <= 20:
        raise ValueError("Inventory requires 1-20 explicit images")
    seen = set()
    for item in data:
        if not isinstance(item, dict) or set(item) not in ({"image"}, {"local_image"}):
            raise ValueError("Each inventory entry selects exactly one immutable image")
        if "image" in item:
            image_reference(item["image"])
        else:
            import re
            if not isinstance(item["local_image"], str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", item["local_image"]):
                raise ValueError("Local image configuration ID is required")
        key = json.dumps(item, sort_keys=True)
        if key in seen:
            raise ValueError("Duplicate image inventory entry")
        seen.add(key)
    return data


def assess_scan(journal, report):
    image = report["image"]
    key = hashlib.sha256(image.encode()).hexdigest()
    unknown = report["status"] == "UNKNOWN"
    update_alert(journal, key=key + ":scan", code="CVE_SCAN_FAILED", severity="HIGH",
                 detail="Image scan unavailable; vulnerability status is unknown.", active=unknown, image=image)
    if unknown:
        return  # Failed evidence must not resolve an existing vulnerability alert.
    high, critical = report["counts"].get("HIGH", 0), report["counts"].get("CRITICAL", 0)
    update_alert(journal, key=key + ":unrated", code="CVE_UNRATED", severity="WARNING",
                 detail="Findings with unknown severity require manual review.",
                 active=report["counts"].get("UNKNOWN", 0) > 0, image=image,
                 change_token=json.dumps(sorted(f["id"] for f in report["findings"] if f["severity"] == "UNKNOWN")))
    ids = sorted((f["id"], f["package"], f["installed"]) for f in report["findings"] if f["severity"] in {"HIGH", "CRITICAL"})
    update_alert(journal, key=key + ":vulns", code="CVE_HIGH_CRITICAL", severity="CRITICAL" if critical else "HIGH",
                 detail=f"{critical} Critical and {high} High package findings. Review package fixes; test and rescan a replacement.",
                 active=bool(high or critical), image=image, change_token=json.dumps(ids))


def inspection_alerts(journal, *, now=None):
    now = now or stamp()
    with closing(sqlite3.connect(journal, timeout=5)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='security_events'").fetchone():
            return
        rows = list(db.execute("SELECT payload FROM security_events WHERE kind='inspection' ORDER BY id DESC LIMIT 1000"))
    rules = set()
    for (encoded,) in rows:
        event = json.loads(encoded)
        at = datetime.fromisoformat(event["at"].replace("Z", "+00:00"))
        if event["verdict"] == "DENY" and 0 <= (now-at).total_seconds() <= 60:
            rules.add(event["rule"])
    # One active alert covers a burst; no raw request information is included.
    update_alert(journal, key="inspection:denied", code="INSPECTION_DENIED", severity="WARNING",
                 detail="Proxy denied requests in the last minute: " + ", ".join(sorted(rules)) if rules else "No recent request denials.",
                 active=bool(rules), change_token=",".join(sorted(rules)))


def scan_cycle(inventory, journal, directory, *, scanner=run_scan, progress=None, stop=None):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    reports = []
    for index, item in enumerate(inventory):
        if stop is not None and stop.is_set():
            break
        local = "local_image" in item
        image = item.get("local_image") or item["image"]
        try:
            report = scanner(image, directory, cache_directory=directory / "cache", **({"local": True} if local else {}))
        except Exception:
            report = {"image": "local://"+image if local else image, "status": "UNKNOWN", "counts": {},
                      "findings": [], "automatic_patching": False, "detail": "Scan failed; retry before assessing this image."}
        record(journal, "image_scan", report)
        assess_scan(journal, report)
        path = directory / (hashlib.sha256(report["image"].encode()).hexdigest()[:20] + ".json")
        path.write_text(json.dumps(report, indent=2)+"\n", encoding="utf-8")
        reports.append(report)
        if progress:
            progress(index+1, len(inventory))
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--interval-seconds", type=int, default=86400)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--stop", action="store_true", help="Request shutdown after the current bounded scan")
    args = parser.parse_args()
    if args.stop:
        args.directory.mkdir(parents=True, exist_ok=True)
        (args.directory / "stop.request").touch()
        return 0
    if args.inventory is None:
        parser.error("--inventory is required to start monitoring")
    if not 300 <= args.interval_seconds <= 7 * 86400:
        parser.error("Scan interval must be between five minutes and seven days")
    inventory = load_inventory(args.inventory)
    args.directory.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    key = hashlib.sha256(str(args.journal.resolve()).encode()).hexdigest()
    lock = LocalTargetCoordinator(args.journal.resolve().parent / ".security-monitor-locks")
    target = {"cluster_id": "local-security-monitor", "namespace": "local", "kind": "Deployment", "name": key}
    current = {}
    def progress(done, total):
        current.update(status="SCANNING", completed=done, total=total,
                       interval_seconds=args.interval_seconds, next_scan_at=None)
        record(args.journal, "monitor", dict(current))
        print(f"CVE scan progress: {done}/{total}", flush=True)
    try:
        with lock.hold(target), ThreadPoolExecutor(max_workers=1) as pool:
            (args.directory / "stop.request").unlink(missing_ok=True)
            (args.directory / "process.json").write_text(json.dumps({"pid": os.getpid(), "journal": str(args.journal.resolve())}), encoding="utf-8")
            due, heartbeat, future = time.monotonic(), 0, None
            while not stop.is_set():
                if (args.directory / "stop.request").exists():
                    stop.set()
                    break
                inspection_alerts(args.journal)
                if future is None and time.monotonic() >= due:
                    inventory = load_inventory(args.inventory)
                    progress(0, len(inventory))
                    future = pool.submit(scan_cycle, inventory, args.journal, args.directory, progress=progress, stop=stop)
                if future is not None and future.done():
                    reports = future.result()
                    next_scan = stamp() + timedelta(seconds=args.interval_seconds)
                    current.update(status="WAITING", completed=len(reports), total=len(reports),
                        interval_seconds=args.interval_seconds, next_scan_at=next_scan.isoformat().replace("+00:00", "Z"))
                    record(args.journal, "monitor", dict(current))
                    print("Scan cycle complete; next scan " + next_scan.isoformat(), flush=True)
                    if args.once:
                        return 2 if any(r["status"] == "UNKNOWN" for r in reports) else 0
                    due, future = time.monotonic() + args.interval_seconds, None
                if current and time.monotonic() >= heartbeat:
                    record(args.journal, "monitor", dict(current))
                    heartbeat = time.monotonic() + 15
                stop.wait(2)
            record(args.journal, "monitor", {"status": "STOPPED", "completed": 0, "total": len(inventory),
                   "interval_seconds": args.interval_seconds, "next_scan_at": None})
    except TargetBusy:
        parser.exit(2, "A security monitor already owns this journal.\n")


if __name__ == "__main__":
    raise SystemExit(main())
