"""Compile and check the hash-locked requirement files.

Each lock is compiled from a loose .in file with pip-tools and records the
SHA-256 of its inputs (the .in file plus any file it includes with -r or -c),
so a lock that was not recompiled after an input change is detected offline.

    python scripts/ci/locks.py --check     # offline: every lock is fresh and hashed
    python scripts/ci/locks.py --compile   # needs pip-tools and network access

Compile with Python 3.12, the CI version. pip-tools records the hash of every
published file of a pinned version, so one lock serves Linux and Windows.
"""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
# Order matters: requirements-dev.in is constrained by the agent lock.
LOCKS = (
    ("requirements.in", "requirements.lock"),
    ("agent/datasets/requirements.in", "agent/datasets/requirements.lock"),
    ("requirements-dev.in", "requirements-dev.lock"),
)
DIGEST_LINE = re.compile(r"^# input-sha256: ([0-9a-f]{64})$", re.MULTILINE)
INCLUDE = re.compile(r"^\s*-[rc]\s+(\S+)", re.MULTILINE)
COMPILE_COMMAND = "python scripts/ci/locks.py --compile"


def _text(path):
    # Line endings differ between Windows and Linux checkouts; hash LF text.
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def input_digest(source, root=ROOT):
    """SHA-256 over the .in file and every file it includes, in include order."""
    digest = hashlib.sha256()
    pending, seen = [Path(root, source).resolve()], set()
    while pending:
        path = pending.pop(0)
        if path in seen:
            continue
        seen.add(path)
        text = _text(path)
        digest.update(path.relative_to(Path(root).resolve()).as_posix().encode() + b"\0")
        digest.update(text.encode("utf-8") + b"\0")
        pending.extend((path.parent / name).resolve() for name in INCLUDE.findall(text))
    return digest.hexdigest()


def requirement_entries(text):
    """Yield (requirement line, its continuation options) for each locked entry."""
    entry = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        if raw.startswith((" ", "\t")):
            if entry is None:
                raise ValueError("continuation line without a requirement")
            entry[1].append(line.strip().rstrip("\\").strip())
            continue
        if entry is not None:
            yield entry[0], entry[1]
        entry = (line.rstrip("\\").strip(), [])
    if entry is not None:
        yield entry[0], entry[1]


PINNED = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^=\s;]+(\s*;.*)?$")
URL_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]* @ https://\S+$")
HASH = re.compile(r"^--hash=sha256:[0-9a-f]{64}$")


def lock_problems(text):
    """Every entry is an exact pin (or an https URL) with at least one sha256 hash."""
    problems, names = [], set()
    for requirement, options in requirement_entries(text):
        if requirement.startswith("-"):
            problems.append(f"option line not allowed in a lock: {requirement}")
            continue
        if not (PINNED.match(requirement) or URL_PIN.match(requirement)):
            problems.append(f"not an exact pin: {requirement}")
        if not options or not all(HASH.match(option) for option in options):
            problems.append(f"missing or malformed --hash: {requirement}")
        names.add(re.split(r"[=@ ;]", requirement, maxsplit=1)[0].lower().replace("_", "-"))
    if not names:
        problems.append("lock has no requirements")
    return problems


def pins(text):
    result = {}
    for requirement, _options in requirement_entries(text):
        name, _, version = requirement.partition("==")
        result[name.strip().lower().replace("_", "-")] = version.split(";")[0].strip() or requirement
    return result


def check(root=ROOT):
    problems = []
    locked = {}
    for source, target in LOCKS:
        lock_path = Path(root, target)
        if not lock_path.is_file():
            problems.append(f"{target}: missing; run {COMPILE_COMMAND}")
            continue
        text = _text(lock_path)
        found = DIGEST_LINE.search(text)
        if not found:
            problems.append(f"{target}: no input-sha256 header")
        elif found.group(1) != input_digest(source, root):
            problems.append(f"{target}: stale; {source} changed. Run {COMPILE_COMMAND}")
        problems.extend(f"{target}: {problem}" for problem in lock_problems(text))
        locked[target] = pins(text)
    # The locks are installed together, so a shared package needs one version.
    seen = {}
    for target, entries in locked.items():
        for name, version in entries.items():
            if name in seen and seen[name][1] != version:
                problems.append(f"{name}: {seen[name][0]} pins {seen[name][1]} but {target} pins {version}")
            seen.setdefault(name, (target, version))
    return problems


def compile_all(root=ROOT):
    for source, target in LOCKS:
        subprocess.run([sys.executable, "-m", "piptools", "compile", "--quiet", "--generate-hashes",
                        "--allow-unsafe", "--strip-extras", "--no-emit-index-url", "--no-emit-trusted-host",
                        "--newline", "lf", "--output-file", target, source],
                       cwd=root, check=True, env={**os.environ, "CUSTOM_COMPILE_COMMAND": COMPILE_COMMAND})
        path = Path(root, target)
        text = _text(path)
        text = DIGEST_LINE.sub("", text).lstrip("\n")
        path.write_text(f"# input-sha256: {input_digest(source, root)}\n{text}", encoding="utf-8", newline="\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--compile", action="store_true")
    args = parser.parse_args(argv)
    if args.compile:
        compile_all()
    problems = check()
    for problem in problems:
        print(f"lock: {problem}", file=sys.stderr)
    if not problems:
        print(f"lock: {len(LOCKS)} lock files are hashed, pinned and fresh")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
