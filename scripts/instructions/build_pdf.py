"""Build GROUP05_INSTRUCTIONS.pdf from the single Markdown source.

    python scripts/instructions/build_pdf.py
    python scripts/instructions/build_pdf.py --version v1.0.0 --repository-url <url> --members "<names>"

The source (docs/instructions/GROUP05_INSTRUCTIONS.md) is checked first with the
same rules as extract_commands.py --check; a source with a missing script or flag
is not built. Include markers are resolved, and the result is rendered with the
standard-library renderer in scripts/instructions/pdf.py, so no PDF tool has to be
installed. Output: dist/GROUP05_INSTRUCTIONS.pdf (the name is fixed) and, with
--markdown, build/instructions/GROUP05_INSTRUCTIONS.md with the includes resolved.

The cover shows the group, course, version, source revision, repository and team
members. The version comes from --version, else a VERSION file at the repository
root, else 'unreleased'. Anything not supplied prints as PLACEHOLDER, and
scripts/submission/check.py fails a PDF that still contains it.
"""

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.instructions import pdf, source  # noqa: E402

OUTPUT_NAME = "GROUP05_INSTRUCTIONS.pdf"
PLACEHOLDER = "[to be added by the team]"
GROUP, COURSE = "Group 05", "CSC3104"
TITLE = "Sentinel: installation and running instructions"


def version(root, given=None):
    if given:
        return given
    path = Path(root, "VERSION")
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    return "unreleased"


def revision(root, given=None):
    if given:
        return given
    try:
        result = subprocess.run(["git", "rev-parse", "--short=12", "HEAD"], cwd=root, capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def describe(info):
    """The label printed above a code block: step, OS, time limit and needs."""
    language, attributes, _errors = source.parse_info(info)
    if "rehearse" not in attributes:
        return ""
    parts = [f"Step {attributes['rehearse']}", attributes.get("os", "?")]
    seconds = attributes.get("timeout", "")
    if seconds.isdigit():
        parts.append(f"up to {int(seconds) // 60} min" if int(seconds) >= 120 else f"up to {seconds} s")
    needs = [item for item in attributes.get("needs", "").split(",") if item]
    if needs:
        parts.append("needs " + ", ".join(needs))
    if attributes.get("optional") == "yes":
        parts.append("only if needed")
    return " | ".join(parts)


def build(root=ROOT, output_dir=None, version_text=None, revision_text=None, repository_url=None, members=None,
          markdown=False):
    """Check, resolve and render the source. Returns (pdf path, problems); nothing is written on a problem."""
    root = Path(root)
    problems = source.check(root)
    if problems:
        return None, problems
    _text, resolved = source.load(root)
    current = version(root, version_text)
    cover = [
        ("bold", 22, TITLE),
        ("regular", 13, f"{GROUP}, {COURSE}"),
        ("regular", 11, f"Version: {current}"),
        ("regular", 11, f"Source revision: {revision(root, revision_text)}"),
        ("regular", 11, f"Repository: {repository_url or PLACEHOLDER}"),
        ("regular", 11, f"Team members: {members or PLACEHOLDER}"),
        ("italic", 9, f"Generated from {source.SOURCE} by scripts/instructions/build_pdf.py. Every command block is "
                      "tagged with a step id; scripts/instructions/extract_commands.py lists them for a rehearsal."),
    ]
    info = {"Title": TITLE, "Subject": f"{GROUP} {COURSE} instructions, version {current}",
            "Producer": "scripts/instructions/build_pdf.py", "SentinelVersion": current}
    data, unencodable = pdf.render(resolved, cover, f"{OUTPUT_NAME} - {current}", info, describe)
    if unencodable:
        return None, ["characters the PDF fonts cannot print: " + " ".join(f"U+{ord(c):04X}" for c in unencodable)]
    target = Path(output_dir) if output_dir else root / "dist"
    target.mkdir(parents=True, exist_ok=True)
    path = target / OUTPUT_NAME
    path.write_bytes(data)
    if markdown:
        folder = root / "build" / "instructions"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / Path(source.SOURCE).name).write_text(resolved, encoding="utf-8")
    return path, []


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    parser.add_argument("--output-dir", type=Path, help="folder for the PDF (default: dist/)")
    parser.add_argument("--version", dest="version_text", help="version for the cover (default: VERSION file)")
    parser.add_argument("--revision", dest="revision_text", help="source revision for the cover (default: git HEAD)")
    parser.add_argument("--repository-url", help="repository or release URL for the cover")
    parser.add_argument("--members", help="team members for the cover, as one string")
    parser.add_argument("--markdown", action="store_true", help="also write the resolved Markdown under build/")
    args = parser.parse_args(argv)
    path, problems = build(args.root, args.output_dir, args.version_text, args.revision_text, args.repository_url,
                           args.members, args.markdown)
    for problem in problems:
        print("FAIL: " + problem)
    if path:
        print(f"Wrote {path}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
