"""Build and verify a versioned Windows handoff ZIP from an exact Git commit."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from uuid import uuid4
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def run(arguments, *, cwd=ROOT, capture=False):
    result = subprocess.run(arguments, cwd=cwd, text=True, encoding="utf-8",
                            errors="replace", stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {arguments[0]} "
                           + (result.stdout[-4000:] if capture else ""))
    return result.stdout.strip() if capture else None


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="0.4.0")
    parser.add_argument("--output", type=Path, default=ROOT / "runs/releases")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.version):
        parser.error("Use a numeric major.minor.patch version")
    for tool in ("git", "docker"):
        if not shutil.which(tool):
            parser.error(f"{tool} must be on PATH")
    if run(["docker", "info", "--format", "{{.OSType}}"], capture=True) != "linux":
        parser.error("Start Docker Desktop with Linux containers")

    commit = run(["git", "rev-parse", "HEAD"], capture=True)
    branch = run(["git", "branch", "--show-current"], capture=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = args.output.resolve() / f"{stamp}-{commit[:8]}-{uuid4().hex[:6]}"
    name = f"sentinel-boutique-runner-{args.version}-linux-amd64"
    bundle = output / name
    bundle.mkdir(parents=True)
    source = bundle / "source"
    source_archive = output / "source.zip"
    print(f"Container release: {output}", flush=True)
    # Archive tracked files from the commit; never copy credentials, run data,
    # personal proposal files or unrelated uncommitted work into a handoff.
    run(["git", "archive", "--format=zip", "--output", str(source_archive), commit])
    with zipfile.ZipFile(source_archive) as archive:
        archive.extractall(source)
    source_archive.unlink()

    runner = f"sentinel-boutique-runner:{args.version}"
    observer = "sentinel-observer:0.3.0"
    workload = "sentinel-otel-validation:0.1.0"
    collector = "otel/opentelemetry-collector:0.163.2"
    builds = [(runner, ["-f", "infra/demo-runner.Dockerfile", "."]),
              (observer, ["-f", "services/observer.Dockerfile", "."]),
              (workload, ["demo/otel"])]
    for image, context in builds:
        print(f"Building {image} from {commit[:12]}...", flush=True)
        run(["docker", "build", "--platform=linux/amd64", "--label", f"org.opencontainers.image.revision={commit}",
             "-t", image, *context], cwd=source)
    run(["docker", "pull", "--platform=linux/amd64", collector])

    print("Running contracts and the complete Python suite inside the app image...", flush=True)
    for title, command in [("contracts", ["scripts/check_contracts.py"]),
                           ("python-tests", ["-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"])]:
        result = run(["docker", "run", "--rm", "--entrypoint", "python", runner, *command], capture=True)
        (bundle / f"{title}.log").write_text(result + "\n", encoding="utf-8")
        print(result[-1500:], flush=True)

    images = []
    for image in (runner, observer, workload, collector):
        info = json.loads(run(["docker", "image", "inspect", image], capture=True))[0]
        if info["Os"] != "linux" or info["Architecture"] != "amd64":
            raise RuntimeError("Unexpected image platform: " + image)
        images.append({"tag": image, "id": info["Id"], "platform": "linux/amd64"})
    print("Exporting four container images...", flush=True)
    run(["docker", "image", "save", "--output", str(bundle / "images.tar"),
         runner, observer, workload, collector])
    metadata = {"version": args.version, "branch": branch, "commit": commit,
                "created_at": datetime.now(timezone.utc).isoformat(), "images": images,
                "validation": {"image_contracts": "PASS", "image_python_tests": "PASS",
                               "frontend_build_and_tests": "PASS (Docker build stage)"},
                "scope": "Local lab; Boutique Kubernetes path plus a separate connected OTel validation lab"}
    (bundle / "release.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    (bundle / "Load-Images.ps1").write_text('''$ErrorActionPreference = 'Stop'
$command = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $command) { $command = Get-Command docker -ErrorAction Stop }
& $command.Source load --input (Join-Path $PSScriptRoot 'images.tar')
if ($LASTEXITCODE -ne 0) { throw 'Container image import failed.' }
$release = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'release.json') -Raw | ConvertFrom-Json
foreach ($image in $release.images) {
    $actual = & $command.Source image inspect $image.tag --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0 -or $actual.Trim() -ne $image.id) { throw ('Image identity mismatch: ' + $image.tag) }
}
Write-Host 'All four image identities verified.'
''', encoding="utf-8")
    (bundle / "Start-App.ps1").write_text(f'''[CmdletBinding()]
param([ValidateRange(1024,65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'Load-Images.ps1')
& (Join-Path $PSScriptRoot 'source/scripts/demo-start.ps1') -Image '{runner}' -SkipBuild -Review -Port $Port
''', encoding="utf-8")
    (bundle / "Stop-App.ps1").write_text('''$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'source/scripts/demo-stop.ps1')
''', encoding="utf-8")
    (bundle / "README.txt").write_text(f'''Sentinel container handoff {args.version} (Linux amd64)
Branch: {branch}
Commit: {commit}

1. Extract this ZIP. Start Docker Desktop with its Linux engine.
2. Install kind and have it on PATH. Use Windows PowerShell.
3. Run: powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\Start-App.ps1
4. Open http://127.0.0.1:8765 (or pass -Port 8766).
5. Stop using Ctrl+C in that terminal, or run:
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\Stop-App.ps1

The launcher loads and checks the included images, runs the recovery scenarios,
then leaves the dashboard open. Existing reserved demo resources are refused;
stop your previous packaged demo explicitly before starting a replacement.
Importing this bundle alone does not stop a running container.

Docker Desktop and kind are host prerequisites. First startup downloads the
Kubernetes node, shipping-service and probe images if necessary. This is not a
fully offline installer. Host Python and Node are not needed for the app.

Buddy 2 connected telemetry validation is a separate, disposable lab:
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\\Load-Images.ps1
  Set-Location .\\source
  python scripts/test_observer_otel_live.py --skip-build
This command additionally requires host Python 3.12+ and kubectl on PATH.
It checks real traces/metrics/logs, HTTP faults with ready Pods, durable delivery,
and Observer replacement. See source/docs/observer-telemetry.md for details.

CHANGES
- Multi-workload Kubernetes monitoring with independent persistent checkpoints.
- Bounded OTLP JSON receiver for traces, metrics and logs, with Bearer auth.
- Current-Pod UID/service correlation, privacy projection and coverage reporting.
- Fresh gauge thresholds; cached/missing values do not prove failure or recovery.
- Atomic incident context/outbox storage and duplicate-safe Coordinator delivery.
- SDK/Collector validation workloads, deployment generator and live test.
- Persistent app review mode (-Review) and explicit image selection (-Image).

Boutique tracing instrumentation, shared-cloud deployment and authenticated
distributed transport remain pending. The new OTLP lab is separate from Boutique.

release.json records exact image IDs and the source commit. SHA256SUMS.txt checks
the source, launchers, logs and image archive. No user proposal, Git credentials,
Kubernetes credentials, .env files or prior run evidence are included.
''', encoding="utf-8")
    files = sorted(p for p in bundle.rglob("*") if p.is_file())
    (bundle / "SHA256SUMS.txt").write_text("".join(
        f"{digest(path)}  {path.relative_to(bundle).as_posix()}\n" for path in files), encoding="utf-8")
    destination = output / (name + ".zip")
    print("Compressing and checking the ZIP...", flush=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=5, allowZip64=True) as archive:
        for path in sorted(p for p in bundle.rglob("*") if p.is_file()):
            archive.write(path, path.relative_to(output))
    with zipfile.ZipFile(destination) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError("ZIP verification failed: " + bad)
    checksum = digest(destination)
    destination.with_suffix(".zip.sha256").write_text(f"{checksum}  {destination.name}\n", encoding="utf-8")
    print(json.dumps({"zip": str(destination), "bytes": destination.stat().st_size,
                      "sha256": checksum, "commit": commit}, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"package: {error}", file=sys.stderr)
        raise SystemExit(1)
