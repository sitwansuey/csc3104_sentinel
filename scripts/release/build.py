"""Build, scan and package the Sentinel-owned release images.

.github/workflows/release.yml runs this on a GitHub-hosted runner from a
version tag. The same commands can run locally as a pre-check, but only the
CI build is the release artefact: builds are not bit-reproducible, so a local
image ID will differ.

For each image in scripts/release/policy.py IMAGES:

1. docker buildx build --platform linux/amd64 --load, with OCI version,
   revision and source labels; the build fails if the labels did not land
   or the image is not linux/amd64.
2. docker image save to a scratch tarball.
3. The scan gate with the pinned Trivy: a High or Critical finding that has a
   fix fails the release unless .trivyignore waives it.
4. A full Trivy JSON report (every severity, fixed or not) and a CycloneDX SBOM.
5. The tarball, gzipped, into the output folder.

Trivy's vulnerability database is downloaded once, so every image is checked
against the same database. Every image is scanned even after one fails, so a
single run shows all findings. release-manifest.json and SHA256SUMS then
cover every file in the output folder. Nothing is pushed or published.

  python scripts/release/build.py --tag v1.0.0-rc.1 --out dist --scratch <empty folder>
"""

import argparse
from collections import Counter
from datetime import UTC, datetime
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release.policy import (IMAGES, PLATFORM, TRIVY_IMAGE, WAIVER_FILE, ReleaseError, parse_waivers,
                                    read_version, tag_version)

SEVERITIES = ("CRITICAL", "HIGH", "MEDIUM", "LOW", "UNKNOWN")
SUMS = "SHA256SUMS"
MANIFEST = "release-manifest.json"
BUILD_TIMEOUT = 3600
SCAN_TIMEOUT = 1200
LOCAL_SOURCE = "local-build"


def run_command(args, *, capture=False, timeout=600):
    """Run one command; output streams to the log unless captured."""
    return subprocess.run(args, cwd=ROOT, capture_output=capture, text=True, encoding="utf-8",
                          check=False, timeout=timeout)


def source_url(environ):
    """The repository URL on a GitHub runner; a local build says it is local."""
    if environ.get("GITHUB_REPOSITORY"):
        return f"{environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{environ['GITHUB_REPOSITORY']}"
    return LOCAL_SOURCE


def labels(image, version, revision, source=LOCAL_SOURCE):
    return {"org.opencontainers.image.title": image.title,
            "org.opencontainers.image.version": version,
            "org.opencontainers.image.revision": revision,
            "org.opencontainers.image.source": source}


def build_command(image, version, revision, root=ROOT, source=LOCAL_SOURCE):
    command = ["docker", "buildx", "build", "--platform", PLATFORM, "--pull", "--load", "--provenance=false",
               "--file", image.dockerfile, "--tag", f"{image.name}:{version}"]
    for key, value in labels(image, version, revision, source).items():
        command += ["--label", f"{key}={value}"]
    if re.search(r"^ARG SOURCE_REVISION\b", Path(root, image.dockerfile).read_text(encoding="utf-8"), re.M):
        command += ["--build-arg", f"SOURCE_REVISION={revision}"]
    return command + ["."]


def trivy_command(args, *, scratch, out, cache, root=ROOT):
    """The pinned Trivy in a locked-down container that sees only its own folders."""
    identity = "10001:10001" if os.name == "nt" else f"{os.getuid()}:{os.getgid()}"
    return ["docker", "run", "--rm", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--user", identity, "--env", "TMPDIR=/scan/tmp",
            "--mount", f"type=bind,source={scratch},target=/scan",
            "--mount", f"type=bind,source={out},target=/out",
            "--mount", f"type=bind,source={cache},target=/cache",
            "--mount", f"type=bind,source={Path(root, WAIVER_FILE)},target=/policy/trivyignore,readonly",
            TRIVY_IMAGE, *args]


def scan_args(kind, archive, output):
    common = ["image", "--input", f"/scan/{archive}", "--cache-dir", "/cache", "--skip-db-update",
              "--timeout", "15m", "--output", f"/out/{output}"]
    if kind == "gate":
        return common + ["--scanners", "vuln", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
                         "--ignorefile", "/policy/trivyignore", "--format", "json", "--exit-code", "1"]
    if kind == "report":
        return common + ["--scanners", "vuln", "--severity", ",".join(reversed(SEVERITIES)),
                         "--list-all-pkgs", "--format", "json", "--exit-code", "0"]
    if kind == "sbom":
        return common + ["--format", "cyclonedx"]
    raise ValueError(kind)


def finding_counts(report, waived=()):
    """Severity counts, fixable High/Critical, and which waivers matched a finding."""
    counts, fixable, matched = Counter(), 0, set()
    for result in report.get("Results") or []:
        for finding in result.get("Vulnerabilities") or []:
            severity = finding.get("Severity", "UNKNOWN")
            counts[severity if severity in SEVERITIES else "UNKNOWN"] += 1
            has_fix = finding.get("Status") == "fixed" or ("Status" not in finding and finding.get("FixedVersion"))
            if severity in {"HIGH", "CRITICAL"} and has_fix:
                if finding.get("VulnerabilityID") in waived:
                    matched.add(finding["VulnerabilityID"])
                else:
                    fixable += 1
    return {name.lower(): counts[name] for name in SEVERITIES}, fixable, sorted(matched)


def inspect(image_ref, runner):
    result = runner(["docker", "image", "inspect", "--format", "{{json .}}", image_ref], capture=True, timeout=60)
    if result.returncode:
        raise ReleaseError(f"cannot inspect {image_ref}")
    return json.loads(result.stdout)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def write_sums(out):
    """SHA256SUMS in sha256sum format, over every other file in `out`."""
    files = sorted(path for path in Path(out).iterdir() if path.is_file() and path.name != SUMS)
    text = "".join(f"{sha256(path)}  {path.name}\n" for path in files)
    Path(out, SUMS).write_bytes(text.encode("utf-8"))
    return [path.name for path in files]


def gzip_file(source, target):
    with Path(source).open("rb") as reader, Path(target).open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as writer:
            shutil.copyfileobj(reader, writer, 1 << 20)
    Path(source).unlink()


def build_image(image, version, revision, *, scratch, out, cache, root, runner, waived, source):
    ref = f"{image.name}:{version}"
    base = f"{image.name}-{version}"
    if runner(build_command(image, version, revision, root, source), timeout=BUILD_TIMEOUT).returncode:
        raise ReleaseError(f"build failed for {image.name}")
    details = inspect(ref, runner)
    found = (details.get("Config") or {}).get("Labels") or {}
    expected = labels(image, version, revision, source)
    wrong = {key: found.get(key) for key, value in expected.items() if found.get(key) != value}
    if wrong or (details.get("Os"), details.get("Architecture")) != ("linux", "amd64"):
        raise ReleaseError(f"{ref} has wrong labels {wrong} or platform {details.get('Os')}/{details.get('Architecture')}")
    archive = f"{base}.tar"
    if runner(["docker", "image", "save", "--output", str(Path(scratch, archive)), ref], timeout=900).returncode:
        raise ReleaseError(f"docker save failed for {image.name}")
    scan = {"scratch": scratch, "out": out, "cache": cache, "root": root}
    gate = runner(trivy_command(scan_args("gate", archive, f"{base}.gate.json"), **scan), timeout=SCAN_TIMEOUT)
    if gate.returncode not in (0, 1):
        raise ReleaseError(f"Trivy gate could not scan {image.name}; no clean result can be asserted")
    for kind, output in (("report", f"{base}.trivy.json"), ("sbom", f"{base}.cdx.json")):
        if runner(trivy_command(scan_args(kind, archive, output), **scan), timeout=SCAN_TIMEOUT).returncode:
            raise ReleaseError(f"Trivy {kind} failed for {image.name}")
    report_path = Path(out, f"{base}.trivy.json")
    if not report_path.is_file():
        raise ReleaseError(f"Trivy report missing for {image.name}")
    counts, fixable, matched = finding_counts(json.loads(report_path.read_text(encoding="utf-8")), waived)
    gzip_file(Path(scratch, archive), Path(out, f"{archive}.gz"))
    return {"name": image.name, "tag": ref, "dockerfile": image.dockerfile, "image_id": details.get("Id"),
            "os": details.get("Os"), "architecture": details.get("Architecture"),
            "labels": {key: found[key] for key in sorted(found) if key.startswith("org.opencontainers.")},
            "archive": f"{archive}.gz", "sbom": f"{base}.cdx.json", "report": f"{base}.trivy.json",
            "gate_report": f"{base}.gate.json", "gate": "pass" if gate.returncode == 0 else "fail",
            "counts": counts, "unwaived_fixable_high_critical": fixable, "waivers_used": matched}


def scanner_metadata(runner, *, scratch, out, cache, root):
    if runner(trivy_command(["image", "--download-db-only", "--cache-dir", "/cache"],
                            scratch=scratch, out=out, cache=cache, root=root), timeout=SCAN_TIMEOUT).returncode:
        raise ReleaseError("cannot download the Trivy vulnerability database")
    result = runner(trivy_command(["version", "--format", "json", "--cache-dir", "/cache"],
                                  scratch=scratch, out=out, cache=cache, root=root), capture=True,
                    timeout=120)
    if result.returncode:
        raise ReleaseError("cannot read the Trivy version")
    info = json.loads(result.stdout)
    return {"image": TRIVY_IMAGE, "version": info.get("Version"),
            "db_updated_at": (info.get("VulnerabilityDB") or {}).get("UpdatedAt"),
            "gate": "HIGH and CRITICAL findings with a fix fail unless waived in .trivyignore"}


def release(tag, revision, out, scratch, cache, *, runner=run_command, root=ROOT, today=None, environ=None):
    """Build every image, write the manifest and checksums; return the failed image names."""
    version = read_version(root)
    if tag_version(tag) != version:
        raise ReleaseError(f"tag {tag} does not match VERSION {version}")
    waivers, problems = parse_waivers(Path(root, WAIVER_FILE).read_text(encoding="utf-8"),
                                      today or datetime.now(UTC).date())
    if problems:
        raise ReleaseError("invalid waivers: " + "; ".join(problems))
    for folder in (out, scratch, cache, Path(scratch, "tmp")):
        Path(folder).mkdir(parents=True, exist_ok=True)
    if any(Path(out).iterdir()):
        raise ReleaseError(f"{out} must be empty so SHA256SUMS covers only this build")
    paths = {"scratch": Path(scratch).resolve(), "out": Path(out).resolve(), "cache": Path(cache).resolve(),
             "root": Path(root).resolve()}
    environ = os.environ if environ is None else environ
    scanner = scanner_metadata(runner, **paths)
    images = [build_image(image, version, revision, runner=runner, waived={waiver.id for waiver in waivers},
                          source=source_url(environ), **paths)
              for image in IMAGES]
    run_url = None
    if environ.get("GITHUB_RUN_ID"):
        run_url = (f"{environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
                   f"{environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{environ['GITHUB_RUN_ID']}")
    manifest = {"schema": "sentinel-release-manifest-v1", "tag": tag, "version": version,
                "source_revision": revision, "platform": PLATFORM, "ci_run_url": run_url, "scanner": scanner,
                "waivers": [{"id": waiver.id, "expires": waiver.expires.isoformat(), "issue": waiver.issue}
                            for waiver in waivers],
                "images": images}
    Path(out, MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    write_sums(out)
    if environ.get("GITHUB_STEP_SUMMARY"):
        with Path(environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary(manifest))
    return [image["name"] for image in images if image["gate"] != "pass"]


def summary(manifest):
    """A Markdown table of the gate result and counts per image."""
    lines = [f"Trivy {manifest['scanner']['version']}, database {manifest['scanner']['db_updated_at']}", "",
             "| Image | Gate | Critical | High | Medium | Unwaived fixable High/Critical | Image ID |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for image in manifest["images"]:
        counts = image["counts"]
        lines.append(f"| {image['tag']} | {image['gate']} | {counts['critical']} | {counts['high']} | "
                     f"{counts['medium']} | {image['unwaived_fixable_high_critical']} | {image['image_id']} |")
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", required=True, help="Release tag; must match VERSION")
    parser.add_argument("--revision", help="Source revision for the labels (default: git HEAD)")
    parser.add_argument("--out", required=True, type=Path, help="Empty folder for the release assets")
    parser.add_argument("--scratch", required=True, type=Path, help="Folder for uncompressed tarballs and Trivy temp")
    parser.add_argument("--cache", type=Path, help="Trivy cache folder (default: <scratch>/cache)")
    args = parser.parse_args(argv)
    revision = args.revision
    if not revision:
        head = run_command(["git", "rev-parse", "HEAD"], capture=True, timeout=30)
        revision = head.stdout.strip() if not head.returncode else ""
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        print("build: cannot determine a 40-hex source revision", file=sys.stderr)
        return 1
    try:
        failed = release(args.tag, revision, args.out, args.scratch, args.cache or Path(args.scratch, "cache"))
    except ReleaseError as error:
        print(f"build: {error}", file=sys.stderr)
        return 1
    if failed:
        print("build: scan gate failed (unwaived fixable High/Critical) for: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"build: {len(IMAGES)} images built, scanned and packaged in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
