"""A small synthetic RCAEval dataset root in the layout the adapter expects.

<root>/cases.json             filtered index rows, as the downloader saves them
<root>/source-manifest.json   pinned source and per-file sizes and sha256
<root>/raw/<case>/metrics.parquet and inject_time.txt (when telemetry=True)

Every Online Boutique case spikes one metric of its root-cause service at the
inject time, so a working ranking method puts that service first. The rows
also hold one Sock Shop case and one RE3 code-fault case that must never be
sampled. No real dataset content is used.
"""

import hashlib
import json
import math
from pathlib import Path

REVISION = "afeacb11bcc94dadfd1c8f483ee4377b2b8b614e"
SERVICES = ("adservice", "cartservice", "currencyservice")
FAULTS = {"RE1": ("cpu", "mem"), "RE2": ("cpu", "socket")}
REPETITIONS = {"RE1": 3, "RE2": 2}
SYSTEMS = {"ob": "Online Boutique", "ss": "Sock Shop", "tt": "Train Ticket"}
INJECT = 1_700_000_000
WINDOW = 600
STEP = 5


def row(suite, system, service, fault, repetition, index=0):
    return {"case": f"{suite.lower()}{system}_{service}_{fault}_{repetition}", "suite": suite, "system": system,
            "system_name": SYSTEMS[system], "dataset": f"{suite}-{system.upper()}", "fault": fault,
            "root_cause_service": service, "repetition": repetition, "inject_time": INJECT + index * 10_000,
            "has_logs": False, "has_traces": False, "has_root_cause_file": False}


def rows(services=SERVICES, faults=None, repetitions=None):
    faults = faults or FAULTS
    repetitions = repetitions or REPETITIONS
    result = []
    for suite, suite_faults in faults.items():
        for service in services:
            for fault in suite_faults:
                for repetition in range(1, repetitions[suite] + 1):
                    result.append(row(suite, "ob", service, fault, repetition, len(result)))
    result.append(row("RE1", "ss", "carts", "cpu", 1, len(result)))
    return result


def config(index_rows):
    counts = {}
    for item in index_rows:
        counts[item["dataset"]] = counts.get(item["dataset"], 0) + 1
    return {"repo_id": "phamquiluan/RCAEval", "revision": REVISION, "suites": ["RE1", "RE2"], "systems": SYSTEMS,
            "faults": ["cpu", "mem", "disk", "delay", "loss", "socket"], "expected_counts": counts}


def metrics_table(item):
    """One synthetic metrics frame: the root cause's fault metric rises by 5 at the inject time."""
    import pyarrow as pa
    inject = item["inject_time"]
    times = list(range(inject - WINDOW, inject + WINDOW + 1, STEP))
    columns = {"time": times}
    indicator = "mem" if item["fault"] == "mem" else "cpu"
    names = sorted({item["root_cause_service"], *SERVICES, "frontend"})
    for offset, service in enumerate(names):
        for kind in ("cpu", "mem", "latency-50", "latency-90"):
            phase = offset * 7 + len(kind)
            columns[f"{service}_{kind}"] = [
                10.0 + 0.2 * math.sin(i * 1.3 + phase) + 0.1 * math.cos(i * 0.7 + phase)
                + (5.0 if service == item["root_cause_service"] and kind == indicator and t >= inject else 0.0)
                for i, t in enumerate(times)]
    return pa.table(columns)


def build(root, index_rows=None, *, telemetry=True):
    """Write the dataset root and return (root, index rows, adapter config)."""
    root = Path(root)
    index_rows = rows() if index_rows is None else index_rows
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases.json").write_bytes((json.dumps(index_rows, indent=2) + "\n").encode("utf-8"))
    files = []
    allowed = [item for item in index_rows if item["suite"] in ("RE1", "RE2")]
    for item in allowed:
        case_dir = root / "raw" / item["case"]
        if telemetry:
            import pyarrow.parquet as pq
            case_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(metrics_table(item), case_dir / "metrics.parquet")
            (case_dir / "inject_time.txt").write_text(str(item["inject_time"]), encoding="utf-8")
            sources = ["metrics.parquet", "inject_time.txt"]
            for name in sources:
                payload = (case_dir / name).read_bytes()
                files.append({"path": f"{item['case']}/{name}", "bytes": len(payload),
                              "sha256": hashlib.sha256(payload).hexdigest(), "git_blob_sha1": None})
        else:
            for name, size in (("metrics.parquet", 4_000_000), ("inject_time.txt", 10)):
                files.append({"path": f"{item['case']}/{name}", "bytes": size, "sha256": "0" * 64,
                              "git_blob_sha1": None})
    manifest = {"repo_id": "phamquiluan/RCAEval", "revision": REVISION, "license": "mit", "gated": False,
                "case_count": len(allowed), "files": files, "total_bytes": sum(f["bytes"] for f in files)}
    (root / "source-manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
    return root, index_rows, config(index_rows)


def write_config(path, value):
    path = Path(path)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
