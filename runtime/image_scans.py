"""The trusted reader for the CVE gate: scan summaries and reviewed exceptions from local files.

The Guard's CVE gate (services/image_security.gate, P5.02) reads scans only as
live_state.image_scans, which this module builds. They are then part of the
Guard's inputs_sha256, so a changed scan or exception voids an earlier ALLOW.
Nothing here scans, calls a cluster or writes a file.
"""

from datetime import datetime
import json
from pathlib import Path

from services.image_security import MAX_GATE_SCANS, canonical_image


EXCEPTIONS_SCHEMA = "sentinel-cve-exceptions-v1"
MAX_SCAN_FILES = 512
MAX_SCAN_FILE_BYTES = 4 * 1024 * 1024


def load_cve_exceptions(path):
    """The exception list from a reviewed exceptions file such as deploy/security/cve-exceptions.json."""
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"schema", "exceptions"} or data["schema"] != EXCEPTIONS_SCHEMA:
        raise ValueError("Expected a " + EXCEPTIONS_SCHEMA + " file")
    return data["exceptions"]


def _rank(scan):
    """Completed scans rank above failed ones, then newer above older; None when the date is unusable."""
    if scan.get("status") == "UNKNOWN":
        return (0, "")
    stamp = scan.get("scanned_at")
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        return None
    return (1, parsed)


def _project(scan):
    """Only the fields the gate reads, so live_state stays small."""
    findings = scan.get("findings", [])
    projected = {"image": scan["image"], "status": scan.get("status"),
                 "findings": [{"id": item.get("id"), "severity": item.get("severity")}
                              if isinstance(item, dict) else item for item in findings]
                 if isinstance(findings, list) else findings}
    for key in ("scanner_image", "scanned_at"):
        if key in scan:
            projected[key] = scan[key]
    return projected


def newest_scans(directory, images=None):
    """{canonical image: newest usable scan summary} from directory/*.json, for images (all when None).

    A digest with no completed scan gets its newest failed (UNKNOWN) record if
    there is one. Unreadable or undated files are skipped, so they can only
    make a scan missing, never clean.
    """
    wanted = None if images is None else {canonical_image(image) for image in images} - {None}
    files = sorted(Path(directory).glob("*.json"))
    if len(files) > MAX_SCAN_FILES:
        raise ValueError("The scan directory holds too many files")
    best = {}
    for path in files:
        try:
            if path.stat().st_size > MAX_SCAN_FILE_BYTES:
                continue
            scan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        image = canonical_image(scan.get("image")) if isinstance(scan, dict) else None
        if image is None or (wanted is not None and image not in wanted):
            continue
        rank = _rank(scan)
        if rank is not None and (image not in best or rank > best[image][0]):
            best[image] = (rank, scan)
    return {image: scan for image, (_, scan) in sorted(best.items())}


def read_image_scans(directory, images, exceptions=None):
    """live_state.image_scans for images: {"scans": [...], "exceptions": [...]}.

    directory holds scan summaries as scripts/security/scan_image_security.py
    and watch_security.py write them; exceptions is the reviewed exceptions
    file, or None for no exceptions.
    """
    if len({canonical_image(image) for image in images} - {None}) > MAX_GATE_SCANS:
        raise ValueError("Too many images for one gate decision")
    scans = [_project(scan) for scan in newest_scans(directory, images).values()]
    return {"scans": scans, "exceptions": load_cve_exceptions(exceptions) if exceptions is not None else []}
