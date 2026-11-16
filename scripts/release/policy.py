"""Shared release rules: version format, the image build list and scan waivers.

preflight.py, build.py and notes.py read these, and so does
.github/workflows/release.yml through them, so the list of images and the
version rules live in one place.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The scanner pin is shared with the security lab, so both use one Trivy release.
from scripts.security.scan_image_security import TRIVY_VERSION

TRIVY_IMAGE = TRIVY_VERSION

PLATFORM = "linux/amd64"
WAIVER_FILE = ".trivyignore"
WAIVER_MAX_DAYS = 90
# A release needs a current, recorded bundle of each class whose code has not
# changed since. Doc-only commits after the evidence are allowed.
REQUIRED_EVIDENCE = ("connected-harness", "agent", "guard-rejection", "security-lab", "guard-eval")
CODE_PATHS = ("services", "runtime", "experiments", "agent", "shared", "scripts", "deploy", "dashboard",
              "requirements.txt", "requirements.lock", "requirements.in")

VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?")
WAIVER = re.compile(r"([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+)\s+exp:(\d{4}-\d{2}-\d{2})")
ISSUE = re.compile(r"(?:issues/|#)(\d+)\b")


class ReleaseError(RuntimeError):
    """A release input is malformed; nothing is built or published."""


@dataclass(frozen=True)
class Image:
    name: str
    dockerfile: str
    title: str


# Every Sentinel-owned Dockerfile under deploy/docker. Third-party images
# (Online Boutique, kind nodes, the grpcurl probe) are not built here.
IMAGES = (
    Image("sentinel-boutique-runner", "deploy/docker/demo-runner.Dockerfile", "Sentinel Boutique demo runner"),
    Image("sentinel-coordinator", "deploy/docker/coordinator.Dockerfile", "Sentinel Coordinator"),
    Image("sentinel-guard", "deploy/docker/guard.Dockerfile", "Sentinel Guard CLI"),
    Image("sentinel-observer", "deploy/docker/observer.Dockerfile", "Sentinel Observer"),
    Image("sentinel-security-inspector", "deploy/docker/security-inspector.Dockerfile",
          "Sentinel security inspector"),
    Image("sentinel-worker", "deploy/docker/worker.Dockerfile", "Sentinel worker"),
)


def version_key(version):
    """Sort key for 'X.Y.Z' or 'X.Y.Z-rc.N'; a final release sorts after its candidates."""
    match = VERSION.fullmatch(version)
    if not match:
        raise ReleaseError(f"version {version!r} is not X.Y.Z or X.Y.Z-rc.N")
    major, minor, patch, candidate = match.groups()
    return (int(major), int(minor), int(patch), int(candidate) if candidate else float("inf"))


def tag_version(tag):
    """The version a tag names, or ReleaseError for anything but vX.Y.Z[-rc.N]."""
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise ReleaseError(f"tag {tag!r} must look like v1.2.3 or v1.2.3-rc.1")
    version_key(tag[1:])
    return tag[1:]


def read_version(root=ROOT):
    path = Path(root, "VERSION")
    if not path.is_file():
        raise ReleaseError("VERSION file is missing")
    text = path.read_text(encoding="utf-8").strip()
    version_key(text)
    return text


@dataclass(frozen=True)
class Waiver:
    id: str
    expires: date
    issue: int


def parse_waivers(text, today):
    """Waivers and problems from a .trivyignore file.

    Each entry is 'ID exp:YYYY-MM-DD', at most WAIVER_MAX_DAYS ahead and not
    expired, under a comment block that names the issue tracking it (#123 or
    an issues/123 link). A blank line ends a block, so consecutive entries can
    share one comment. Trivy itself stops honouring an entry after its date.
    """
    waivers, problems, comments = [], [], []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            comments = []
            continue
        if line.startswith("#"):
            comments.append(line[1:])
            continue
        match = WAIVER.fullmatch(line)
        issue = next((found for comment in comments for found in ISSUE.findall(comment)), None)
        if not match:
            problems.append(f"{WAIVER_FILE}:{number}: expected 'ID exp:YYYY-MM-DD', got {line!r}")
            continue
        try:
            expires = date.fromisoformat(match.group(2))
        except ValueError:
            problems.append(f"{WAIVER_FILE}:{number}: invalid expiry date {match.group(2)}")
            continue
        if issue is None:
            problems.append(f"{WAIVER_FILE}:{number}: {match.group(1)} has no issue link in the comment above it")
        if expires < today:
            problems.append(f"{WAIVER_FILE}:{number}: {match.group(1)} expired on {expires}")
        elif expires > today + timedelta(days=WAIVER_MAX_DAYS):
            problems.append(f"{WAIVER_FILE}:{number}: {match.group(1)} expires more than {WAIVER_MAX_DAYS} days ahead")
        waivers.append(Waiver(match.group(1), expires, int(issue) if issue else 0))
    return waivers, problems


def image_problems(root=ROOT):
    """Listed Dockerfiles that are missing, and Sentinel Dockerfiles not listed."""
    listed = {image.dockerfile for image in IMAGES}
    problems = [f"{path} is listed but missing" for path in sorted(listed) if not Path(root, path).is_file()]
    present = {path.relative_to(root).as_posix() for path in Path(root, "deploy/docker").glob("*.Dockerfile")}
    problems += [f"{path} is not in the release image list" for path in sorted(present - listed)]
    names = [image.name for image in IMAGES]
    if len(set(names)) != len(names):
        problems.append("image names are not unique")
    return problems
