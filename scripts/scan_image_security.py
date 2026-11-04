"""Scan a digest-pinned remote image; publish recommendations without changes.

Requires Docker Desktop. The scanner receives no Docker socket, kubeconfig,
registry credentials, repository source or writable application mount.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.security_store import record
from services.image_security import image_reference, summarize


# Trivy 0.76.1, reviewed release manifest. Updating it is a source change.
TRIVY_VERSION = "aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"


def run_scan(image, directory, *, runner=subprocess.run):
    image_reference(image)
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    # Resolve this explicit release once, then execute only the immutable digest.
    pull = runner(["docker", "pull", TRIVY_VERSION], capture_output=True, timeout=300, check=False)
    if pull.returncode:
        raise RuntimeError("Cannot pull the configured Trivy release")
    inspection = runner(["docker", "image", "inspect", TRIVY_VERSION], capture_output=True,
                        timeout=30, check=False)
    if inspection.returncode:
        raise RuntimeError("Cannot resolve scanner image identity")
    digests = json.loads(inspection.stdout)[0].get("RepoDigests", [])
    matches = [value for value in digests if value.startswith("aquasec/trivy@sha256:")]
    if len(matches) != 1:
        raise RuntimeError("Scanner manifest digest is ambiguous")
    scanner = image_reference(matches[0])
    # A private temporary mount also prevents using a stale report after failure.
    with tempfile.TemporaryDirectory(prefix="sentinel-scan-", dir=directory) as temporary:
        output = Path(temporary)
        (output / "empty.yaml").write_text("{}\n", encoding="utf-8")
        (output / "ignore").write_text("", encoding="utf-8")
        identity = "10001:10001" if os.name == "nt" else f"{os.getuid()}:{os.getgid()}"
        command = ["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--user", identity,
                   "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
                   "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
                   "--mount", f"type=bind,source={output},target=/report", scanner,
                   "image", "--config", "/report/empty.yaml", "--ignorefile", "/report/ignore",
                   "--cache-dir", "/report/cache", "--image-src", "remote", "--scanners", "vuln",
                   "--severity", "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL", "--ignore-unfixed=false",
                   "--list-all-pkgs", "--format", "json", "--output", "/report/raw.json",
                   "--timeout", "5m", "--exit-code", "0", image]
        process = runner(command, capture_output=True, timeout=360, check=False)
        if process.returncode:
            raise RuntimeError("CVE scan unavailable; no clean result can be asserted")
        raw = output / "raw.json"
        if not raw.is_file() or raw.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("Scanner report is missing or exceeds 32 MiB")
        report = summarize(json.loads(raw.read_text(encoding="utf-8")), image)
        report["scanner_image"] = scanner
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, help="Remote repository@sha256:manifest-digest")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--journal", type=Path, help="Optional dashboard telemetry SQLite path")
    args = parser.parse_args(argv)
    image_reference(args.image)
    try:
        report = run_scan(args.image, args.output.resolve().parent)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError):
        report = {"image": args.image, "status": "UNKNOWN", "scanner": "Trivy",
                  "counts": {}, "findings": [], "automatic_patching": False,
                  "detail": "Scan failed or evidence was incomplete. Retry; this is not a clean scan."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.journal:
        record(args.journal, "image_scan", report)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "output": str(args.output)}))
    return 2 if report["status"] == "UNKNOWN" else 1 if report["status"] == "FINDINGS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
