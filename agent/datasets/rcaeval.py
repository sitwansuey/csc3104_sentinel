"""Pinned RCAEval import with strict non-code selection and verified resumable files."""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "data/experimentation/sources/rcaeval.json"
FILENAMES = {"metrics.parquet", "logs.parquet", "traces.parquet", "inject_time.txt", "root_cause.txt"}


def settings():
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def parquet():
    try:
        import pyarrow.parquet as pq
        return pq
    except ImportError as error:
        raise RuntimeError("Install the data reader: python -m pip install -r requirements.txt") from error


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def fetch(url, limit=16 * 1024 * 1024):
    with urlopen(Request(url, headers={"User-Agent": "Sentinel-RCAEval-importer"}), timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Remote metadata exceeded its size limit")
    return data


def select_cases(rows, config):
    selected, seen = [], set()
    for row in rows:
        suite = row.get("suite")
        if suite not in config["suites"]:
            continue
        system, fault = row.get("system"), row.get("fault")
        if system not in config["systems"] or fault not in config["faults"]:
            raise ValueError("Unexpected system or code/unknown fault in an allowed suite")
        if suite == "RE1" and fault == "socket":
            raise ValueError("Unexpected socket fault in RE1")
        service, repeat = row.get("root_cause_service"), row.get("repetition")
        if not isinstance(service, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", service):
            raise ValueError("Invalid service identifier")
        if type(repeat) is not int or repeat < 1:
            raise ValueError("Invalid repetition")
        expected = f"{suite.lower()}{system}_{service}_{fault}_{repeat}"
        if (row.get("case") != expected or row.get("dataset") != f"{suite}-{system.upper()}"
                or row.get("system_name") != config["systems"][system]):
            raise ValueError("Case name and system/suite/fault metadata disagree")
        if expected in seen:
            raise ValueError("Duplicate case: " + expected)
        seen.add(expected)
        selected.append(row)
    counts = dict(Counter(r["dataset"] for r in selected))
    if counts != config["expected_counts"]:
        raise ValueError(f"Pinned dataset counts changed: {counts}")
    return sorted(selected, key=lambda r: r["case"])


def file_plan(cases, siblings):
    chosen = {r["case"]: r for r in cases}
    files = {}
    for item in siblings:
        relative = item["rfilename"]
        parts = relative.split("/")
        if parts[0] not in chosen:
            continue
        if len(parts) != 2 or parts[1] not in FILENAMES:
            raise ValueError("Unexpected selected-case path: " + relative)
        if type(item.get("size")) is not int or item["size"] < 0:
            raise ValueError("Missing source size: " + relative)
        sha = item.get("lfs", {}).get("sha256")
        blob = item.get("blobId")
        if not ((sha and re.fullmatch(r"[0-9a-f]{64}", sha)) or
                (not sha and isinstance(blob, str) and re.fullmatch(r"[0-9a-f]{40}", blob))):
            raise ValueError("Missing source checksum: " + relative)
        if relative in files:
            raise ValueError("Duplicate source path")
        files[relative] = {"path": relative, "bytes": item["size"], "sha256": sha, "git_blob_sha1": blob if not sha else None}
    for case, row in chosen.items():
        expected = {"metrics.parquet", "inject_time.txt"}
        for flag, filename in [("has_logs", "logs.parquet"), ("has_traces", "traces.parquet"),
                               ("has_root_cause_file", "root_cause.txt")]:
            if row.get(flag):
                expected.add(filename)
        actual = {p.split("/")[1] for p in files if p.startswith(case + "/")}
        if actual != expected:
            raise ValueError("Telemetry availability disagrees with the index: " + case)
    return sorted(files.values(), key=lambda item: item["path"])


def checked_path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Path escapes the dataset directory")
    return path


def verify(path, item):
    if not path.is_file() or path.stat().st_size != item["bytes"]:
        return False
    digest = hashlib.sha256() if item["sha256"] else hashlib.sha1()
    if not item["sha256"]:
        digest.update(f"blob {item['bytes']}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest() == (item["sha256"] or item["git_blob_sha1"])


def download_file(root, item, base_url):
    destination = checked_path(root, item["path"])
    if verify(destination, item):
        return item["path"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    for attempt in range(3):
        try:
            # A fresh query on retries avoids stale expired CDN redirect signatures.
            url = base_url + item["path"] + "?download=true"
            if attempt:
                url += "&retry=" + str(time.time_ns())
            with urlopen(Request(url, headers={"User-Agent": "Sentinel-RCAEval-importer"}), timeout=90) as response, temporary.open("wb") as stream:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > item["bytes"]:
                        raise ValueError("Download exceeds declared source size")
                    stream.write(chunk)
            if not verify(temporary, item):
                raise ValueError("Source checksum mismatch: " + item["path"])
            temporary.replace(destination)
            return item["path"]
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def validate_local_plan(root, config):
    plan = json.loads((root / "source-manifest.json").read_text(encoding="utf-8"))
    if plan.get("repo_id") != config["repo_id"] or plan.get("revision") != config["revision"]:
        raise ValueError("Local dataset is from another source/revision; use a new output directory")
    cases = select_cases(json.loads((root / "cases.json").read_text(encoding="utf-8")), config)
    # Recheck allowed paths even when preparing offline; local metadata is not trusted blindly.
    names = {r["case"] for r in cases}
    for item in plan["files"]:
        parts = item["path"].split("/")
        if len(parts) != 2 or parts[0] not in names or parts[1] not in FILENAMES:
            raise ValueError("Excluded or unsafe case in local manifest")
    return plan, cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "data/experimentation/datasets/rcaeval-non-code")
    parser.add_argument("--index-only", action="store_true", help="Fetch/filter metadata without downloading telemetry")
    parser.add_argument("--case", help="Download one allowed case; default downloads all 645")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= 8:
        parser.error("workers must be 1..8")
    config, root = settings(), args.output.resolve()
    try:
        pq = parquet()
        base = f"https://huggingface.co/datasets/{config['repo_id']}/resolve/{config['revision']}/"
        rows = pq.read_table(io.BytesIO(fetch(base + "cases.parquet"))).to_pylist()
        cases = select_cases(rows, config)
        info = json.loads(fetch(f"https://huggingface.co/api/datasets/{config['repo_id']}/revision/{config['revision']}?blobs=true"))
        if info.get("sha") != config["revision"]:
            raise ValueError("Remote revision does not match the pinned source")
        files = file_plan(cases, info["siblings"])
        if args.case and args.case not in {r["case"] for r in cases}:
            raise ValueError("Case is not an allowed non-code RE1/RE2 case")
        # Reject mixed roots rather than silently keeping previously downloaded code faults.
        if (root / "source-manifest.json").exists():
            validate_local_plan(root, config)
        allowed_paths = {f["path"] for f in files}
        if (root / "raw").exists():
            for path in (root / "raw").rglob("*"):
                relative = path.relative_to(root / "raw").as_posix()
                resumable_partial = relative.endswith(".part") and relative[:-5] in allowed_paths
                if path.is_file() and relative not in allowed_paths and not resumable_partial:
                    raise ValueError("Output contains excluded/unrecognized raw data: " + str(path))
        root.mkdir(parents=True, exist_ok=True)
        save(root / "cases.json", cases)
        plan = {"repo_id": config["repo_id"], "revision": config["revision"], "license": "MIT (upstream dataset card)",
                "selection": {"suites": config["suites"], "systems": config["systems"], "faults": config["faults"]},
                "case_count": len(cases), "counts": dict(Counter(r["dataset"] for r in cases)),
                "files": files, "total_bytes": sum(f["bytes"] for f in files)}
        save(root / "source-manifest.json", plan)
        print(f"Selected {len(cases)} non-code cases, {len(files)} files, {plan['total_bytes'] / 1e9:.2f} GB; RE3 excluded.", flush=True)
        if args.index_only:
            print("Filtered index: " + str(root / "cases.json"))
            return 0
        selected = [f for f in files if not args.case or f["path"].startswith(args.case + "/")]
        state = {"status": "downloading", "requested_files": len(selected), "verified_files": 0,
                 "scope": args.case or "all non-code cases", "errors": []}
        save(root / "download-status.json", state)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(download_file, root / "raw", item, base): item["path"] for item in selected}
            for future in as_completed(futures):
                try:
                    future.result()
                    state["verified_files"] += 1
                except Exception as error:
                    state["errors"].append({"file": futures[future], "error": str(error)})
                completed = state["verified_files"] + len(state["errors"])
                if completed % 25 == 0 or completed == len(selected):
                    save(root / "download-status.json", state)
                    print(f"Checked {completed}/{len(selected)} files; {len(state['errors'])} errors", flush=True)
        state["status"] = "complete" if not state["errors"] else "incomplete"
        save(root / "download-status.json", state)
        print("Dataset: " + str(root))
        return 0 if not state["errors"] else 1
    except (OSError, ValueError, RuntimeError) as error:
        print(f"RCAEval import failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
