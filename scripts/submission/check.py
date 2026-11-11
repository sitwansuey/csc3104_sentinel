"""Submission readiness gate: is every milestone-5 deliverable present?

    python scripts/submission/check.py
    python scripts/submission/check.py --video-file <local recording> --remote --format markdown

Reads docs/submission/deliverables.json, which the team fills in, and checks:

  release-tag         the tag is set and exists locally (--remote: also on origin, read-only)
  instructions-pdf    dist/GROUP05_INSTRUCTIONS.pdf exists under that exact name, is a PDF with
                      pages, has no '[to be added by the team]' left and carries the tag as its version
  article-pdf         the article PDF is set and exists
  article-pages       within article_max_pages (MANUAL while the limit is unconfirmed or unreadable)
  video-link          an https link to the uploaded video is recorded
  video-file          with --video-file: ffprobe duration within video_seconds, an audio stream
                      and at least 1080 lines (MANUAL without --video-file)
  rehearsal:<tier>    a PASS rehearsal record for each required tier, on the tag's commit

Each check prints PASS, FAIL or MANUAL with a reason. The manifest's 'manual' items
(course policy, deadline, access, upload) are things only a person can do; they
always print as MANUAL and never pass automatically. The exit code is 1 while any
check FAILs, 0 when none does, and 2 when the manifest itself is unusable.
Nothing is written, uploaded or changed; external tools are only read from.
"""

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.instructions.build_pdf import OUTPUT_NAME, PLACEHOLDER  # noqa: E402

MANIFEST = "docs/submission/deliverables.json"
SCHEMA = "sentinel-submission-v1"
REHEARSAL_SCHEMA = "sentinel-rehearsal-v1"
TIERS = ("linux-container", "windows-fresh", "teammate-laptop")
PAGE = re.compile(rb"/Type\s*/Page(?!s)\b")
PDF_VERSION = re.compile(rb"/SentinelVersion \(((?:[^()\\]|\\.)*)\)")
MIN_VIDEO_HEIGHT = 1080


class ManifestError(ValueError):
    """The deliverables manifest is missing or malformed."""


@dataclass
class Result:
    status: str  # PASS, FAIL or MANUAL
    check: str
    detail: str


def run_command(args, timeout=60):
    """Run a read-only command; returns (exit code, standard output). A missing tool is exit code 127."""
    try:
        completed = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, ""
    except subprocess.TimeoutExpired:
        return 124, ""
    return completed.returncode, completed.stdout


def load_manifest(root):
    path = Path(root, MANIFEST)
    if not path.is_file():
        raise ManifestError(f"{MANIFEST} is missing")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ManifestError(f"{MANIFEST} is not valid JSON: {error}") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ManifestError(f"{MANIFEST} must be an object with schema {SCHEMA}")
    tiers = data.get("rehearsal_tiers", [])
    if not isinstance(tiers, list) or any(tier not in TIERS for tier in tiers):
        raise ManifestError(f"{MANIFEST}: rehearsal_tiers must list only {', '.join(TIERS)}")
    manual = data.get("manual", [])
    if not isinstance(manual, list) or not all(isinstance(item, dict) and item.get("id") and item.get("text")
                                               for item in manual):
        raise ManifestError(f"{MANIFEST}: each manual item needs an id and a text")
    return data


def tag_commit(root, tag, run):
    """The commit a local tag points to, or None."""
    code, out = run(["git", "-C", str(root), "rev-parse", "-q", "--verify", f"refs/tags/{tag}^{{commit}}"])
    return out.strip() if code == 0 and out.strip() else None


def check_tag(root, manifest, run, remote):
    tag = manifest.get("release_tag")
    if not tag:
        return Result("FAIL", "release-tag", f"release_tag is not set in {MANIFEST}"), None
    commit = tag_commit(root, tag, run)
    if not commit:
        return Result("FAIL", "release-tag", f"tag {tag} does not exist in this clone"), None
    if remote:
        code, out = run(["git", "-C", str(root), "ls-remote", "--tags", "origin", f"refs/tags/{tag}"])
        if code != 0 or not out.strip():
            return Result("FAIL", "release-tag", f"tag {tag} is not on origin"), commit
    where = "here and on origin" if remote else "here (pass --remote to also check origin)"
    return Result("PASS", "release-tag", f"{tag} -> {commit[:12]}, {where}"), commit


def pdf_pages(data):
    """Page count from the PDF bytes, or 0 when the page objects are compressed and cannot be counted."""
    return len(PAGE.findall(data))


def check_instructions(root, manifest):
    relative = manifest.get("instructions_pdf") or f"dist/{OUTPUT_NAME}"
    path = Path(root, relative)
    if path.name != OUTPUT_NAME:
        return Result("FAIL", "instructions-pdf", f"{relative} must be named exactly {OUTPUT_NAME}")
    if not path.is_file():
        return Result("FAIL", "instructions-pdf", f"{relative} is missing; run scripts/instructions/build_pdf.py")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-"):
        return Result("FAIL", "instructions-pdf", f"{relative} is not a PDF")
    pages = pdf_pages(data)
    if pages == 0:
        return Result("FAIL", "instructions-pdf", f"{relative} has no readable pages")
    if PLACEHOLDER.encode("latin-1") in data:
        return Result("FAIL", "instructions-pdf", f"the cover still says {PLACEHOLDER}; rebuild with "
                                                  "--repository-url and --members")
    found = PDF_VERSION.search(data)
    version = found.group(1).decode("latin-1") if found else None
    tag = manifest.get("release_tag")
    if not tag:
        return Result("FAIL", "instructions-pdf", f"{pages} pages, version {version}; no release_tag to compare")
    if version != tag:
        return Result("FAIL", "instructions-pdf", f"cover version is {version}, release tag is {tag}")
    return Result("PASS", "instructions-pdf", f"{pages} pages, version {version}")


def check_article(root, manifest):
    relative = manifest.get("article_pdf")
    results = []
    if not relative:
        results.append(Result("FAIL", "article-pdf", f"article_pdf is not set in {MANIFEST}"))
        data = None
    elif not Path(root, relative).is_file():
        results.append(Result("FAIL", "article-pdf", f"{relative} is missing"))
        data = None
    else:
        data = Path(root, relative).read_bytes()
        if data.startswith(b"%PDF-"):
            results.append(Result("PASS", "article-pdf", relative))
        else:
            results.append(Result("FAIL", "article-pdf", f"{relative} is not a PDF"))
            data = None
    limit = manifest.get("article_max_pages")
    if data is None:
        results.append(Result("MANUAL", "article-pages", "no article to count"))
    elif not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        results.append(Result("MANUAL", "article-pages", "article_max_pages is not set; confirm the limit with "
                                                         "the lecturer"))
    else:
        pages = pdf_pages(data)
        if pages == 0:
            results.append(Result("MANUAL", "article-pages", f"page count not readable; check it is at most {limit}"))
        elif pages > limit:
            results.append(Result("FAIL", "article-pages", f"{pages} pages, limit {limit}"))
        else:
            results.append(Result("PASS", "article-pages", f"{pages} pages, limit {limit}"))
    return results


def check_video_link(manifest):
    link = manifest.get("video_link")
    if not link:
        return Result("FAIL", "video-link", f"video_link is not set in {MANIFEST}")
    if not re.match(r"^https://[^\s/]+\S*$", str(link)):
        return Result("FAIL", "video-link", "video_link must be an https URL")
    return Result("PASS", "video-link", "recorded (its content is not checked here)")


def check_video_file(manifest, video_file, run):
    if not video_file:
        return Result("MANUAL", "video-file", "pass --video-file to check duration, audio and resolution")
    if not Path(video_file).is_file():
        return Result("FAIL", "video-file", "the given video file does not exist")
    code, out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,height",
                     "-of", "json", str(video_file)], timeout=120)
    if code == 127:
        return Result("MANUAL", "video-file", "ffprobe is not installed; check duration and audio by hand")
    try:
        probe = json.loads(out) if code == 0 else None
        duration = float(probe["format"]["duration"])
    except (TypeError, KeyError, ValueError):
        return Result("FAIL", "video-file", "ffprobe could not read the file")
    bounds = manifest.get("video_seconds") or {}
    low, high = bounds.get("min"), bounds.get("max")
    streams = probe.get("streams", [])
    problems = []
    if isinstance(low, (int, float)) and duration < low:
        problems.append(f"{duration / 60:.1f} min is shorter than {low / 60:g} min")
    if isinstance(high, (int, float)) and duration > high:
        problems.append(f"{duration / 60:.1f} min is longer than {high / 60:g} min")
    if not any(stream.get("codec_type") == "audio" for stream in streams):
        problems.append("no audio stream")
    heights = [stream.get("height") or 0 for stream in streams if stream.get("codec_type") == "video"]
    if not heights:
        problems.append("no video stream")
    elif max(heights) < MIN_VIDEO_HEIGHT:
        problems.append(f"{max(heights)} lines, below {MIN_VIDEO_HEIGHT}")
    if problems:
        return Result("FAIL", "video-file", "; ".join(problems))
    return Result("PASS", "video-file", f"{duration / 60:.1f} min, audio, {max(heights)} lines")


def read_record(root, relative):
    """(record, problem) for one rehearsal record path."""
    path = Path(root, relative)
    if not path.is_file():
        return None, f"{relative} is missing"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, f"{relative} is not valid JSON"
    if not isinstance(record, dict) or record.get("schema") != REHEARSAL_SCHEMA:
        return None, f"{relative} is not a {REHEARSAL_SCHEMA} record"
    return record, None


def check_rehearsals(root, manifest, commit):
    records, problems = [], []
    for relative in manifest.get("rehearsal_records", []):
        record, problem = read_record(root, relative)
        if problem:
            problems.append(problem)
        else:
            records.append((relative, record))
    results = []
    for tier in manifest.get("rehearsal_tiers", []):
        mine = [(relative, record) for relative, record in records if record.get("tier") == tier]
        passing = [relative for relative, record in mine if record.get("outcome") == "PASS"
                   and commit is not None and record.get("source_revision") == commit]
        if passing:
            results.append(Result("PASS", f"rehearsal:{tier}", ", ".join(passing)))
        elif commit is None and any(record.get("outcome") == "PASS" for _relative, record in mine):
            results.append(Result("FAIL", f"rehearsal:{tier}", "PASS record found, but no release tag to match"))
        elif mine:
            results.append(Result("FAIL", f"rehearsal:{tier}", "no PASS record on the release tag's commit"))
        else:
            results.append(Result("FAIL", f"rehearsal:{tier}", "no record"))
    results.extend(Result("FAIL", "rehearsal-records", problem) for problem in problems)
    return results


def evaluate(root=ROOT, video_file=None, remote=False, run=run_command):
    """Every check result, automated checks first, then the human-only items."""
    manifest = load_manifest(root)
    tag_result, commit = check_tag(root, manifest, run, remote)
    results = [tag_result, check_instructions(root, manifest), *check_article(root, manifest),
               check_video_link(manifest), check_video_file(manifest, video_file, run),
               *check_rehearsals(root, manifest, commit)]
    results.extend(Result("MANUAL", item["id"], item["text"]) for item in manifest.get("manual", []))
    return results


def render(results, markdown=False):
    failures = sum(result.status == "FAIL" for result in results)
    manual = sum(result.status == "MANUAL" for result in results)
    verdict = (f"NOT READY: {failures} check(s) fail" if failures else "Automated checks pass") + \
        f"; {manual} item(s) need a person (MANUAL never passes automatically)"
    if markdown:
        lines = ["| Status | Check | Detail |", "| --- | --- | --- |"]
        lines.extend(f"| {r.status} | {r.check} | {r.detail.replace('|', '/')} |" for r in results)
        return "\n".join(lines + ["", verdict]) + "\n"
    width = max(len(result.check) for result in results)
    lines = [f"{result.status:<6}  {result.check:<{width}}  {result.detail}" for result in results]
    return "\n".join(lines + ["", verdict]) + "\n"


def main(argv=None, run=run_command):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--video-file", type=Path, help="local copy of the final recording, read with ffprobe")
    parser.add_argument("--remote", action="store_true", help="also confirm the tag on origin (git ls-remote)")
    parser.add_argument("--format", choices=("text", "markdown"), default="text")
    args = parser.parse_args(argv)
    try:
        results = evaluate(args.root, args.video_file, args.remote, run)
    except ManifestError as error:
        print("FAIL: " + str(error), file=sys.stderr)
        return 2
    sys.stdout.write(render(results, markdown=args.format == "markdown"))
    return 1 if any(result.status == "FAIL" for result in results) else 0


if __name__ == "__main__":
    sys.exit(main())
