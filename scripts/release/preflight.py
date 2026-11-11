"""Release preflight: a go/no-go table before a human pushes a version tag.

It changes nothing in git or on GitHub. The quality gate writes the usual
ignored local outputs (.coverage, coverage.json), and --notes writes the draft
release notes where you ask.

Checks, in order:

  clean     the working tree has no changes or untracked files
  branch    HEAD is the release base (origin/main by default; fetch first)
  version   VERSION is X.Y.Z or X.Y.Z-rc.N, the tag names it, pyproject.toml
            agrees, and it sorts above every earlier version tag
  tag       the tag does not exist yet (a published tag is never moved)
  ci        the newest Checks run for HEAD succeeded (read-only gh run list)
  evidence  each class in REQUIRED_EVIDENCE has a current, recorded bundle
            whose code paths are unchanged at HEAD; doc-only commits are fine
  waivers   every .trivyignore entry names an issue and expires within 90 days
  images    the build list matches the Sentinel Dockerfiles under deploy/docker
  notes     draft release notes from the merged pull requests since the last tag
  gate      the full quality gate from docs/testing.md

GO needs every check to pass. A failed check gives NO-GO (exit 1); a skipped
check gives INCOMPLETE (exit 2), never GO.

  python scripts/release/preflight.py --tag v1.0.0-rc.1 --notes release-notes.md
  python scripts/release/preflight.py --tag v1.0.0-rc.1 --skip gate ci
  python scripts/release/preflight.py --images
"""

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.release import notes
from scripts.release.policy import (CODE_PATHS, IMAGES, REQUIRED_EVIDENCE, WAIVER_FILE, ReleaseError, image_problems,
                                    parse_waivers, read_version, tag_version, version_key)

CHECKS = ("clean", "branch", "version", "tag", "ci", "evidence", "waivers", "images", "notes", "gate")
CI_WORKFLOW = "checks.yml"
# The quality gate in docs/testing.md, run with this interpreter. pip-audit
# needs the network and runs in CI only.
GATE = (
    ("locks", ["scripts/ci/locks.py", "--check"]),
    ("ruff", ["-m", "ruff", "check", "."]),
    ("ruff baseline", ["scripts/ci/ruff_baseline.py", "--check"]),
    ("except audit", ["scripts/ci/except_audit.py", "--report"]),
    ("mypy", ["-m", "mypy"]),
    ("contracts", ["scripts/check_contracts.py"]),
    ("offline demo", ["experiments/validation/demo_offline_flow.py"]),
    ("unit tests", ["-m", "coverage", "run", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"]),
    ("coverage json", ["-m", "coverage", "json", "-o", "coverage.json"]),
    ("coverage gate", ["scripts/ci/coverage_gate.py", "coverage.json"]),
    ("results tables", ["scripts/evidence/summarize.py", "--check"]),
    ("limitations", ["scripts/evidence/limitations.py", "--strict"]),
    ("doc links", ["scripts/check_doc_links.py"]),
    ("doc citations", ["scripts/check_doc_citations.py"]),
)
GATE_TIMEOUT = 3600


def run_command(args, timeout=120):
    try:
        return subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              check=False, timeout=timeout)
    except FileNotFoundError:
        return subprocess.CompletedProcess(args, 127, "", f"{args[0]} not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timed out after {timeout} s")


@dataclass(frozen=True)
class Row:
    check: str
    status: str  # PASS, FAIL or SKIP
    detail: str


def _tail(result):
    text = (result.stderr or "").strip() or (result.stdout or "").strip()
    return text.splitlines()[-1][:200] if text else f"exit {result.returncode}"


class Preflight:
    def __init__(self, tag, *, runner=run_command, root=ROOT, base="origin/main", today=None, notes_path=None,
                 python=sys.executable):
        self.tag, self.runner, self.root, self.base = tag, runner, Path(root), base
        self.today = today or datetime.now(UTC).date()
        self.notes_path, self.python = notes_path, python

    def git(self, args):
        result = self.runner(["git", *args])
        if result.returncode:
            raise ReleaseError(f"git {' '.join(args[:2])}: {_tail(result)}")
        return result.stdout

    def head(self):
        return self.git(["rev-parse", "HEAD"]).strip()

    def check_clean(self):
        changes = self.git(["status", "--porcelain", "--untracked-files=normal"]).splitlines()
        if changes:
            return "FAIL", f"{len(changes)} uncommitted or untracked path(s), e.g. {changes[0].strip()}"
        return "PASS", "working tree is clean"

    def check_branch(self):
        head, base = self.head(), self.git(["rev-parse", "--verify", f"{self.base}^{{commit}}"]).strip()
        if head != base:
            return "FAIL", f"HEAD {head[:12]} is not {self.base} ({base[:12]}); fetch, then check out the base"
        return "PASS", f"HEAD is {self.base} ({head[:12]})"

    def check_version(self):
        version, wanted = read_version(self.root), tag_version(self.tag)
        if version != wanted:
            return "FAIL", f"VERSION is {version} but the tag names {wanted}"
        project = tomllib.loads(Path(self.root, "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        if project != pep440(version):
            return "FAIL", f"pyproject.toml version {project} should be {pep440(version)}"
        newer = []
        for name in self.git(["tag", "--list", "v*"]).split():
            try:
                if name != self.tag and version_key(tag_version(name)) >= version_key(version):
                    newer.append(name)
            except ReleaseError:
                continue
        if newer:
            return "FAIL", f"{version} does not sort above existing tag(s) {', '.join(sorted(newer))}"
        return "PASS", f"VERSION {version} matches {self.tag}"

    def check_tag(self):
        if self.git(["tag", "--list", self.tag]).strip():
            return "FAIL", f"{self.tag} already exists; never move a tag, prepare the next version instead"
        return "PASS", f"{self.tag} is not taken"

    def check_ci(self):
        head = self.head()
        result = self.runner(["gh", "run", "list", "--commit", head, "--workflow", CI_WORKFLOW, "--limit", "20",
                              "--json", "status,conclusion,url,createdAt"])
        if result.returncode:
            return "FAIL", f"cannot read CI runs: {_tail(result)}"
        try:
            runs = sorted(json.loads(result.stdout), key=lambda run: run.get("createdAt", ""), reverse=True)
        except (ValueError, AttributeError):
            return "FAIL", "unreadable gh run list output"
        if not runs:
            return "FAIL", f"no {CI_WORKFLOW} run for {head[:12]}"
        newest = runs[0]
        if newest.get("status") != "completed":
            return "FAIL", f"newest {CI_WORKFLOW} run is {newest.get('status')}: {newest.get('url')}"
        if newest.get("conclusion") != "success":
            return "FAIL", f"newest {CI_WORKFLOW} run concluded {newest.get('conclusion')}: {newest.get('url')}"
        return "PASS", f"{CI_WORKFLOW} succeeded: {newest.get('url')}"

    def check_evidence(self):
        try:
            index = json.loads(Path(self.root, "evidence/index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return "FAIL", f"cannot read evidence/index.json: {error}"
        problems, fresh = [], []
        for name in REQUIRED_EVIDENCE:
            bundles = [bundle for bundle in index.get("bundles", [])
                       if bundle.get("class") == name and bundle.get("status") == "current"]
            recorded = sorted((bundle for bundle in bundles if bundle.get("provenance") == "recorded"),
                              key=lambda bundle: bundle.get("captured_at", ""), reverse=True)
            if not recorded:
                problems.append(f"{name}: no current recorded bundle ({len(bundles)} current, none recorded)")
                continue
            stale = None
            for bundle in recorded:
                changed = self.changed_code(bundle.get("source_revision", ""))
                if changed == []:
                    fresh.append(f"{name}={bundle['bundle_id']}")
                    break
                stale = stale or (bundle, changed)
            else:
                bundle, changed = stale
                what = ", ".join(changed[:3]) if changed else "the revision is not in this clone"
                problems.append(f"{name}: code changed since {bundle.get('source_revision', '')[:12]} ({what})")
        if problems:
            return "FAIL", "; ".join(problems)
        return "PASS", "; ".join(fresh)

    def changed_code(self, revision):
        """Code paths changed between `revision` and HEAD; None if the revision is unknown."""
        if not revision:
            return None
        result = self.runner(["git", "diff", "--name-only", revision, "HEAD", "--", *CODE_PATHS])
        if result.returncode:
            return None
        return [line for line in result.stdout.splitlines() if line.strip()]

    def check_waivers(self):
        path = Path(self.root, WAIVER_FILE)
        if not path.is_file():
            return "FAIL", f"{WAIVER_FILE} is missing (an empty one means no waivers)"
        waivers, problems = parse_waivers(path.read_text(encoding="utf-8"), self.today)
        if problems:
            return "FAIL", "; ".join(problems)
        return "PASS", f"{len(waivers)} waiver(s), all with an issue and an expiry"

    def check_images(self):
        problems = image_problems(self.root)
        if problems:
            return "FAIL", "; ".join(problems)
        return "PASS", ", ".join(image.name for image in IMAGES)

    def check_notes(self):
        previous = notes.previous_tag(self.git, self.tag)
        pulls, others = notes.merged_pulls(self.git, previous)
        if self.notes_path:
            text = notes.render(self.tag, self.head(), previous, pulls, others,
                                notes.load_json(Path(self.root, "docs/limitations.json")),
                                notes.load_json(Path(self.root, "evidence/index.json")))
            Path(self.notes_path).write_text(text, encoding="utf-8", newline="\n")
        since = previous or "the first commit"
        written = f"; draft written to {self.notes_path}" if self.notes_path else ""
        return "PASS", f"{len(pulls)} merged PR(s) and {others} other commit(s) since {since}{written}"

    def gate_rows(self):
        rows = []
        for name, args in GATE:
            result = self.runner([self.python, *args], GATE_TIMEOUT)
            rows.append(Row(f"gate: {name}", "PASS" if result.returncode == 0 else "FAIL",
                            "ok" if result.returncode == 0 else _tail(result)))
        return rows

    def run(self, selected):
        rows = []
        for name in CHECKS:
            if name not in selected:
                rows.append(Row(name, "SKIP", "skipped on request"))
                continue
            if name == "gate":
                rows += self.gate_rows()
                continue
            try:
                status, detail = getattr(self, f"check_{name}")()
            except ReleaseError as error:
                status, detail = "FAIL", str(error)
            rows.append(Row(name, status, detail))
        return rows


def pep440(version):
    """'1.0.0-rc.1' -> '1.0.0rc1', the form pyproject.toml uses."""
    return version.replace("-rc.", "rc")


def verdict(rows):
    if any(row.status == "FAIL" for row in rows):
        return "NO-GO", 1
    if any(row.status == "SKIP" for row in rows):
        return "INCOMPLETE", 2
    return "GO", 0


def table(rows):
    width = max(len(row.check) for row in rows)
    return "\n".join(f"{row.check.ljust(width)}  {row.status:<4}  {row.detail}" for row in rows)


def _names(values):
    names = {name for value in values or [] for name in value.replace(",", " ").split()}
    unknown = names - set(CHECKS)
    if unknown:
        raise SystemExit(f"preflight: unknown check(s) {', '.join(sorted(unknown))}; choose from {', '.join(CHECKS)}")
    return names


def main(argv=None, runner=run_command, root=ROOT, today=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tag", help="Tag being prepared, e.g. v1.0.0-rc.1")
    parser.add_argument("--base", default="origin/main", help="Revision HEAD must equal (default: origin/main)")
    parser.add_argument("--only", nargs="+", metavar="CHECK", help="Run only these checks")
    parser.add_argument("--skip", nargs="+", metavar="CHECK", help="Skip these checks (the verdict is INCOMPLETE)")
    parser.add_argument("--notes", type=Path, help="Write the draft release notes to this file")
    parser.add_argument("--images", action="store_true", help="Print the image build list as JSON and exit")
    args = parser.parse_args(argv)
    if args.images:
        print(json.dumps([{"name": image.name, "dockerfile": image.dockerfile} for image in IMAGES], indent=2))
        return 0
    if not args.tag:
        parser.error("--tag is required")
    try:
        tag_version(args.tag)
    except ReleaseError as error:
        parser.error(str(error))
    only = _names(args.only)
    selected = (only or set(CHECKS)) - _names(args.skip)
    rows = Preflight(args.tag, runner=runner, root=root, base=args.base, today=today,
                     notes_path=args.notes).run(selected)
    if only:
        # An explicit subset is judged on its own, and the verdict says so.
        rows = [row for row in rows if row.status != "SKIP" or row.check in only]
    word, code = verdict(rows)
    print(table(rows))
    scope = f" (only: {', '.join(name for name in CHECKS if name in only)})" if only else ""
    print(f"\n{word}: {args.tag}{scope}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
