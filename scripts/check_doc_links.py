"""Fail when a Markdown file links to a missing file or a missing heading anchor.

Checks every relative link and '#anchor' in the repository's *.md files.
External links (http, https, mailto) are skipped. Standard library only, offline.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
# Generated, vendored or ignored folders are not documentation; neither are hidden
# tool folders, except .github.
SKIP_DIRS = frozenset({"venv", "node_modules", "__pycache__", "runs", "outputs", "build", "dist"})
INLINE = re.compile(r"!?\[(?:[^\[\]]|\[[^\]]*\])*\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+\"[^\"]*\")?\s*$", re.MULTILINE)
FENCE = re.compile(r"^\s{0,3}(```|~~~).*?^\s{0,3}\1[^\n]*$", re.MULTILINE | re.DOTALL)
CODE = re.compile(r"`[^`\n]*`")
HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.MULTILINE)
HTML_ANCHOR = re.compile(r"<a\s+[^>]*(?:name|id)=[\"']([^\"']+)[\"']", re.IGNORECASE)
EXTERNAL = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def documentation(relative):
    folders = Path(relative).parts[:-1]
    return not any(part in SKIP_DIRS or (part.startswith(".") and part != ".github") for part in folders)


def markdown_files(root):
    """Tracked and untracked-but-not-ignored *.md files; a plain walk outside git."""
    root = Path(root)
    try:
        listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z", "--", "*.md"], cwd=root,
                                capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        listed = None
    if listed is not None and listed.returncode == 0 and (root / ".git").exists():
        return sorted(root / name for name in listed.stdout.decode("utf-8").split("\0")
                      if name and documentation(name) and (root / name).is_file())
    return sorted(path for path in root.rglob("*.md") if documentation(path.relative_to(root)))


def strip_code(text):
    return CODE.sub("", FENCE.sub("", text))


def slug(heading):
    """GitHub-style anchor for a heading."""
    text = re.sub(r"<[^>]+>", "", heading)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("`", "").strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(path, cache):
    if path not in cache:
        text = FENCE.sub("", path.read_text(encoding="utf-8"))
        seen, result = {}, set()
        for match in HEADING.finditer(text):
            base = slug(match.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            result.add(base if count == 0 else f"{base}-{count}")
        result.update(HTML_ANCHOR.findall(text))
        cache[path] = result
    return cache[path]


def links(text):
    text = strip_code(text)
    return [match.group(1) for match in INLINE.finditer(text)] + REFERENCE.findall(text)


def check(root=ROOT):
    """Return 'file: problem' strings for every dead relative link or anchor."""
    root = Path(root).resolve()
    problems, cache = [], {}
    for source in markdown_files(root):
        for target in links(source.read_text(encoding="utf-8")):
            if EXTERNAL.match(target) or target.startswith("//"):
                continue
            path_part, _, anchor = target.partition("#")
            path_part = unquote(path_part.split("?", 1)[0])
            if not path_part:
                resolved = source
            elif path_part.startswith("/"):
                resolved = (root / path_part.lstrip("/")).resolve()
            else:
                resolved = (source.parent / path_part).resolve()
            label = source.relative_to(root).as_posix()
            if not resolved.exists():
                problems.append(f"{label}: dead link {target}")
                continue
            if anchor and resolved.is_file() and resolved.suffix.lower() == ".md" \
                    and unquote(anchor).lower() not in anchors(resolved, cache):
                problems.append(f"{label}: dead anchor {target}")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    problems = check(args.root)
    for problem in problems:
        print("FAIL: " + problem)
    if not problems:
        print("All relative Markdown links and anchors resolve")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
