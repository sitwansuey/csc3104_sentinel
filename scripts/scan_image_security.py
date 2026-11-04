"""Scan a digest-pinned remote image; publish recommendations without changes.

Requires Docker Desktop. The scanner receives no Docker socket, kubeconfig,
registry credentials, repository source or writable application mount.
"""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.security_store import record
from services.image_security import image_reference, summarize


# Trivy 0.76.1, reviewed release manifest. Updating it is a source change.
TRIVY_VERSION = "aquasec/trivy@sha256:790fd66d1697395d8693d1acaa1ea702406a20f109b6fc7bac8db623eca6c06c"


def archive_config_id(path, image):
    """Bind Docker's index/manifest/config ID to one exported linux/amd64 config.

    Docker's containerd image store may identify an image by its OCI index, while
    Trivy reports the platform configuration ID. Verify the digest chain instead
    of equating these different identities or accepting an arbitrary archive.
    """
    with tarfile.open(path) as archive:
        def read(name):
            member = archive.getmember(name)
            if not member.isfile() or member.size > 1024 * 1024:
                raise ValueError("Invalid archive metadata")
            return archive.extractfile(member).read()
        manifest = json.loads(read("manifest.json"))
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError("Expected one platform image in the archive")
        config_path = manifest[0]["Config"]
        config_bytes = read(config_path)
        config_id = "sha256:" + hashlib.sha256(config_bytes).hexdigest()
        config = json.loads(config_bytes)
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("The security lab scans linux/amd64 candidates")
        def reaches(digest, depth=0):
            if depth > 4 or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
                raise ValueError("Invalid OCI identity graph")
            if digest == config_id:
                return True
            raw = read("blobs/sha256/" + digest.split(":")[1])
            if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError("OCI metadata digest mismatch")
            item = json.loads(raw)
            if "config" in item:
                return item["config"]["digest"] == config_id
            children = [entry for entry in item.get("manifests", []) if entry.get("platform", {}).get("os", "linux") == "linux"
                        and entry.get("platform", {}).get("architecture", "amd64") == "amd64"]
            if len(children) != 1:
                raise ValueError("Ambiguous OCI platform selection")
            return reaches(children[0]["digest"], depth+1)
        if not reaches(image):
            raise ValueError("Exported configuration does not belong to the requested image")
        return config_id


def run_scan(image, directory, *, runner=subprocess.run, local=False, cache_directory=None):
    if local:
        if not isinstance(image, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            raise ValueError("Local scans require an exact Docker image ID")
    else:
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
        (output / "tmp").mkdir()
        if local:
            saved = runner(["docker", "image", "save", "--output", str(output / "image.tar"), image],
                           capture_output=True, timeout=300, check=False)
            if saved.returncode:
                raise RuntimeError("Cannot export the selected local image")
            config_id = archive_config_id(output / "image.tar", image)
        identity = "10001:10001" if os.name == "nt" else f"{os.getuid()}:{os.getgid()}"
        command = ["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--user", identity,
                   "--memory", "2g", "--cpus", "2", "--pids-limit", "256",
                   "--env", "TMPDIR=/report/tmp",
                   "--security-opt=no-new-privileges", "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
                   "--mount", f"type=bind,source={output},target=/report"]
        if cache_directory is not None:
            cache = Path(cache_directory).resolve()
            cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            command += ["--mount", f"type=bind,source={cache},target=/cache"]
        command += [scanner,
                   "image", "--config", "/report/empty.yaml", "--ignorefile", "/report/ignore",
                   "--cache-dir", "/cache" if cache_directory is not None else "/report/cache", "--scanners", "vuln",
                   "--severity", "UNKNOWN,LOW,MEDIUM,HIGH,CRITICAL", "--ignore-unfixed=false",
                   "--list-all-pkgs", "--format", "json", "--output", "/report/raw.json",
                   "--timeout", "5m", "--exit-code", "0"]
        command += ["--input", "/report/image.tar"] if local else ["--image-src", "remote", image]
        process = runner(command, capture_output=True, timeout=360, check=False)
        if process.returncode:
            raise RuntimeError("CVE scan unavailable; no clean result can be asserted")
        raw = output / "raw.json"
        if not raw.is_file() or raw.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("Scanner report is missing or exceeds 32 MiB")
        report = summarize(json.loads(raw.read_text(encoding="utf-8")), config_id if local else image, local=local)
        if local:
            report.update(image="local://" + image, scanned_config_id=config_id)
        report["scanner_image"] = scanner
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image", help="Remote repository@sha256:manifest-digest")
    source.add_argument("--local-image", help="Local immutable Docker image ID; scanned through a verified archive")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--journal", type=Path, help="Optional dashboard telemetry SQLite path")
    args = parser.parse_args(argv)
    image = args.local_image or args.image
    if not args.local_image:
        image_reference(image)
    try:
        report = run_scan(image, args.output.resolve().parent, **({"local": True} if args.local_image else {}))
    except (ValueError, KeyError, TypeError, tarfile.TarError, OSError, RuntimeError, subprocess.SubprocessError):
        report = {"image": "local://" + image if args.local_image else image, "status": "UNKNOWN", "scanner": "Trivy",
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
