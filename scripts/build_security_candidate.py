"""Build/test a patched Boutique shipping candidate; never deploy it."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM_COMMIT = "5b608cb1dfe317d05e4f1bb8c99b7618452fde87"
TAG = "sentinel-shipping-candidate:0.10.7-security.1"


def command(args, timeout=600):
    result = subprocess.run([str(arg) for arg in args], capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result.stdout


def build(directory):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "upstream"
    command(["git", "clone", "--depth", "1", "--branch", "v0.10.7",
             "https://github.com/GoogleCloudPlatform/microservices-demo.git", source])
    if command(["git", "-C", source, "rev-parse", "HEAD"]).strip() != UPSTREAM_COMMIT:
        raise ValueError("Upstream release identity changed")
    context = directory / "context"
    context.mkdir()
    shutil.copytree(source / "src/shippingservice", context / "shipping")
    shutil.copyfile(source / "LICENSE", context / "LICENSE")
    shutil.copyfile(ROOT / "demo/security/shipping-candidate.Dockerfile", context / "Dockerfile")
    # Pin the resolved non-root runtime manifest in the build evidence.
    runtime = "gcr.io/distroless/static:nonroot"
    command(["docker", "pull", runtime])
    inspection = json.loads(command(["docker", "image", "inspect", runtime]))[0]
    candidates = [value for value in inspection["RepoDigests"] if value.startswith("gcr.io/distroless/static@sha256:")]
    if len(candidates) != 1:
        raise ValueError("Ambiguous distroless runtime")
    arguments = ["docker", "build", "--progress=plain", "--platform=linux/amd64", "-t", TAG,
                 "--build-arg", "RUNTIME_IMAGE=" + candidates[0],
                 "--build-arg", "UPSTREAM_COMMIT=" + UPSTREAM_COMMIT, context]
    result = subprocess.run([str(arg) for arg in arguments], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, timeout=900, check=False)
    (directory / "build.log").write_text(result.stdout, encoding="utf-8")
    if result.returncode:
        raise RuntimeError("Candidate build/tests failed; inspect " + str(directory / "build.log"))
    image = json.loads(command(["docker", "image", "inspect", TAG]))[0]
    evidence = {"tag": TAG, "image_id": image["Id"], "upstream_commit": UPSTREAM_COMMIT,
                "runtime_manifest": candidates[0], "go": "1.27.3", "golang.org/x/net": "v0.60.0",
                "upstream_go_tests_passed": True, "deployed": False}
    (directory / "candidate.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.directory), indent=2))


if __name__ == "__main__":
    main()
