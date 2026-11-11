"""Copy one run directory into a sanitised, append-only evidence bundle.

Only files matching the run class's allowlist in profiles.json are copied, and
the code deny-list below always wins over a profile (even '**'). Each file is
sanitised, size-capped and hashed into MANIFEST.json, then the bundle is added
to evidence/index.json. A failure leaves no partial bundle behind.

Example:
  python scripts/evidence/collect.py --run data/experimentation/runs/<run> \
      --class agent --bundle-id <run>
"""

import argparse
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evidence import sanitise  # noqa: E402
from scripts.evidence.sanitise import SanitiseError  # noqa: E402

EVIDENCE = ROOT / "evidence"
PROFILES = Path(__file__).resolve().with_name("profiles.json")
INDEX_SCHEMA = "sentinel-evidence-index-v1"
MANIFEST_SCHEMA = "sentinel-evidence-manifest-v1"
# .gitignore ignores these names at any depth, so a bundle must never use them.
IGNORED_NAMES = frozenset({"runs", "outputs", "build", "dist", "node_modules"})
BUNDLE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{2,95}")
SUMMARY_FILES = ("summary.json", "data/evidence/summary.json", "batch.json", "validation.json", "results.json")

# Code-level deny-list. A profile cannot override it.
DENY_SEGMENT = re.compile(r"pki.*|locks?|node_modules|\.venv", re.IGNORECASE)
DENY_NAME = re.compile(r".*(kubeconfig|credential|secret).*|.*\.(db|sqlite|sqlite3|db-journal|sqlite-wal|sqlite-shm|"
                       r"wal|shm|key|pem|crt|cer|csr|p12|pfx|srl|tar|gz|tgz|zip|lock|request)", re.IGNORECASE)


def denied(relative):
    """True when a path must never be copied, whatever the profile says."""
    parts = PurePosixPath(relative).parts
    if any(part.startswith(".") or part in IGNORED_NAMES for part in parts):
        return True
    if any(DENY_SEGMENT.fullmatch(part) for part in parts[:-1]):
        return True
    return bool(DENY_NAME.fullmatch(parts[-1]))


def glob_regex(pattern):
    """'*' and '?' stay inside one folder; '**' crosses folders."""
    out, index = [], 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            out.append(".*")
            index += 2
        elif pattern[index] == "*":
            out.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            out.append("[^/]")
            index += 1
        else:
            out.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(out))


def allowed(relative, patterns):
    return any(glob_regex(pattern).fullmatch(relative) for pattern in patterns)


def load_profiles(path=PROFILES):
    return json.loads(Path(path).read_text(encoding="utf-8"))["classes"]


def select_files(run_dir, patterns):
    """Sorted relative posix paths that pass the deny-list and the allowlist."""
    run_dir = Path(run_dir)
    selected = []
    for folder, directories, files in os.walk(run_dir, followlinks=False):
        directories[:] = sorted(d for d in directories if not (Path(folder) / d).is_symlink())
        for name in files:
            path = Path(folder) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(run_dir).as_posix()
            if not denied(relative) and allowed(relative, patterns):
                selected.append(relative)
    return sorted(selected)


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


UTC_STAMP = re.compile(r"\d{8}T\d{6}Z")
# Packaged run folders are named from the host's local clock with no zone.
LOCAL_STAMP = re.compile(r"\d{8}-\d{6}")
UTC_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})")


def parse_utc_offset(value):
    """'+08:00' or '-0530' as a timezone; anything else is an error."""
    match = UTC_OFFSET.fullmatch(value or "")
    if not match or int(match.group(2)) > 14 or int(match.group(3)) > 59:
        raise ValueError("UTC offset must look like +08:00 or -05:30")
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    return timezone(timedelta(minutes=-minutes if match.group(1) == "-" else minutes))


def _stamp(value, local_offset=None):
    """UTC time from a folder name. A zone-less local stamp needs the offset of the clock that wrote it."""
    match = UTC_STAMP.search(value)
    if match:
        return datetime.strptime(match.group(0), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    match = LOCAL_STAMP.search(value)
    if match and local_offset is not None:
        local = datetime.strptime(match.group(0), "%Y%m%d-%H%M%S").replace(tzinfo=local_offset)
        return local.astimezone(timezone.utc)
    return None


def captured_at(run_dir, local_offset=None):
    """Best start time from the run's own files, else its folder name, else None.

    A local-time folder stamp is never read as UTC: without local_offset it gives None.
    """
    for name in SUMMARY_FILES:
        value = _read_json(Path(run_dir) / name)
        if isinstance(value, dict):
            for key in ("started_at", "captured_at"):
                if isinstance(value.get(key), str):
                    return value[key]
            attempts = value.get("baseline_attempts")
            if isinstance(attempts, list) and attempts and isinstance(attempts[0], dict) and attempts[0].get("at"):
                return attempts[0]["at"]
    stamp = _stamp(Path(run_dir).name, local_offset)
    return stamp.isoformat().replace("+00:00", "Z") if stamp else None


def recorded_provenance(run_dir):
    for name in SUMMARY_FILES:
        value = _read_json(Path(run_dir) / name)
        if isinstance(value, dict) and isinstance(value.get("provenance"), dict) \
                and value["provenance"].get("source_revision"):
            return value["provenance"]
    return None


def export_coordinator_journal(run_dir):
    """Coordinator records through the public read-only reader; never the database file."""
    database = Path(run_dir) / "coordinator.sqlite"
    if not database.is_file():
        return None
    from services.coordinator.read import read_records
    return read_records(database, limit=600)


EXPORTS = {"coordinator-journal.json": export_coordinator_journal}


def load_index(evidence_root):
    path = Path(evidence_root) / "index.json"
    if not path.exists():
        return {"schema": INDEX_SCHEMA, "bundles": []}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes((json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    os.replace(temporary, path)


def default_context(repo_root=None):
    users = {getpass.getuser(), os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    return sanitise.Context(repo_root=str(repo_root or ROOT), home=str(Path.home()),
                            usernames=tuple(sorted(user for user in users if user)))


def collect(run_dir, run_class, bundle_id, *, evidence_root=EVIDENCE, profiles=None, date=None,
            inferred_revision=None, inferred_basis=None, include_model_io=False, context=None,
            file_cap=sanitise.FILE_CAP_BYTES, bundle_cap=sanitise.BUNDLE_CAP_BYTES, exports=(),
            folder_utc_offset=None):
    """Create evidence_root/<date>/<bundle_id>/ and append it to index.json."""
    run_dir, evidence_root = Path(run_dir), Path(evidence_root)
    profiles = profiles if profiles is not None else load_profiles()
    if run_class not in profiles:
        raise ValueError("Unknown run class: " + str(run_class))
    if not BUNDLE_ID.fullmatch(bundle_id) or bundle_id in IGNORED_NAMES:
        raise ValueError("Bundle id must be lower-case letters, digits, '.', '_' or '-' and not an ignored name")
    if not run_dir.is_dir():
        raise ValueError("Run directory does not exist")
    index = load_index(evidence_root)
    if any(entry["bundle_id"] == bundle_id for entry in index["bundles"]):
        raise FileExistsError("Bundle id already in the index: " + bundle_id)
    local_offset = parse_utc_offset(folder_utc_offset) if folder_utc_offset is not None else None
    started = captured_at(run_dir, local_offset)
    date = date or (started[:10] if started else None)
    if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        if LOCAL_STAMP.search(run_dir.name) and not UTC_STAMP.search(run_dir.name):
            raise ValueError("The run folder name is local time with no zone; pass --folder-utc-offset "
                             "(for example +08:00) or --date YYYY-MM-DD")
        raise ValueError("Could not derive the capture date; pass --date YYYY-MM-DD")
    destination = evidence_root / date / bundle_id
    if destination.exists():
        raise FileExistsError("Destination already exists: " + destination.as_posix())

    context = context or default_context()
    context.include_model_io = include_model_io
    recorded = recorded_provenance(run_dir)
    if recorded:
        block = {"kind": "recorded", **recorded}
    else:
        block = {"kind": "inferred", "source_revision": inferred_revision or "unknown",
                 "basis": inferred_basis or "No provenance block in the run; revision not inferred"}

    outputs, omitted, total = [], [], 0
    sources = [(relative, (run_dir / relative).read_bytes()) for relative in
               select_files(run_dir, profiles[run_class]["include"])]
    for name in exports:
        value = EXPORTS[name](run_dir)
        if value is not None:
            sources.append((name, json.dumps(value).encode("utf-8")))
    for relative, data in sources:
        clean, redactions = sanitise.sanitise_bytes(data, relative, context)
        if len(clean) > file_cap:
            omitted.append({"path": relative, "bytes": len(clean), "reason": "over per-file cap"})
            continue
        total += len(clean)
        if total > bundle_cap:
            raise SanitiseError(f"Bundle exceeds the {bundle_cap} byte cap; narrow the profile")
        outputs.append((relative, clean, redactions))
    if not outputs:
        raise ValueError("No allowlisted files found for class " + run_class)

    manifest = {"schema": MANIFEST_SCHEMA, "bundle_id": bundle_id, "class": run_class,
                "captured_at": started, "source_run_dir_name": run_dir.name,
                "sanitiser_version": sanitise.SANITISER_VERSION, "provenance": block,
                "files": [{"path": relative, "sha256": hashlib.sha256(clean).hexdigest(), "bytes": len(clean),
                           "redactions": redactions} for relative, clean, redactions in outputs],
                "omitted": omitted}
    staging = evidence_root / date / (".staging-" + bundle_id)
    try:
        staging.mkdir(parents=True)
        for relative, clean, _ in outputs:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(clean)
        write_json(staging / "MANIFEST.json", manifest)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    index["bundles"].append({"bundle_id": bundle_id, "class": run_class,
                             "path": destination.relative_to(evidence_root).as_posix(),
                             "captured_at": started, "source_revision": block["source_revision"],
                             "provenance": block["kind"], "status": "current", "reason": None})
    write_json(evidence_root / "index.json", index)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True, type=Path, help="Run directory to collect from (read only)")
    parser.add_argument("--class", dest="run_class", required=True, choices=sorted(load_profiles()))
    parser.add_argument("--bundle-id", required=True)
    parser.add_argument("--evidence-root", type=Path, default=EVIDENCE)
    parser.add_argument("--date", help="Capture date YYYY-MM-DD when the run does not record one")
    parser.add_argument("--folder-utc-offset",
                        help="UTC offset (such as +08:00) of the clock that named a local-time run folder "
                             "(YYYYMMDD-HHMMSS); used only when the run files record no start time")
    parser.add_argument("--repo-root", type=Path, help="Checkout the run came from; its path becomes <repo>")
    parser.add_argument("--inferred-revision", help="Revision for runs that predate provenance stamping")
    parser.add_argument("--inferred-basis", help="How the inferred revision was chosen")
    parser.add_argument("--include-model-io", action="store_true", help="Keep model prompts/responses, 4 KB each")
    parser.add_argument("--export", action="append", default=[], choices=sorted(EXPORTS),
                        help="Also export a journal through its public reader")
    args = parser.parse_args(argv)
    try:
        manifest = collect(args.run, args.run_class, args.bundle_id, evidence_root=args.evidence_root,
                           date=args.date, inferred_revision=args.inferred_revision,
                           inferred_basis=args.inferred_basis, include_model_io=args.include_model_io,
                           context=default_context(args.repo_root), exports=tuple(args.export),
                           folder_utc_offset=args.folder_utc_offset)
    except (SanitiseError, ValueError, FileExistsError) as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 1
    print(f"Collected {len(manifest['files'])} file(s) into bundle {manifest['bundle_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
