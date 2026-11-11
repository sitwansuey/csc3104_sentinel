"""Keep the ruff baseline in pyproject.toml explicit and shrinking.

The baseline is the per-file-ignores block between the BASELINE markers. It
lists, per file, the rules that file broke when the gate was added, so CI is
green without a mass rewrite while new code meets every rule.

    python scripts/ci/ruff_baseline.py --check   # fail on stale or forbidden entries
    python scripts/ci/ruff_baseline.py --write   # regenerate after fixing files

Both need ruff. Blind-except rules are never baselined outside tests/: those
handlers must be fixed or justified (scripts/ci/except_audit.py).
"""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
START, END = "# BASELINE-START", "# BASELINE-END"
NEVER_BASELINED = frozenset({"BLE001", "S110", "E722"})
ENTRY = re.compile(r'^"(?P<path>[^"]+)" = \[(?P<codes>[^\]]*)\]$')


def baseline(text):
    """{path: set(codes)} from the block between the markers."""
    block = text.split(START, 1)[1].split(END, 1)[0]
    result = {}
    for line in block.splitlines():
        found = ENTRY.match(line.strip())
        if found:
            result[found.group("path")] = {code.strip().strip('"') for code in found.group("codes").split(",")
                                            if code.strip()}
    return result


def forbidden(entries):
    """Entries that try to baseline a blind-except rule outside tests/."""
    return sorted(f"{path}: {code}" for path, codes in entries.items() for code in codes
                  if code in NEVER_BASELINED and not path.startswith("tests/"))


def render(entries):
    lines = [f'"{path}" = [{", ".join(chr(34) + code + chr(34) for code in sorted(codes))}]'
             for path, codes in sorted(entries.items())]
    return "\n".join(lines)


def current_violations(root=ROOT):
    """{path: set(codes)} that ruff reports with the baseline switched off."""
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--exit-zero",
         "--config", "lint.per-file-ignores = {}", "."],
        cwd=root, capture_output=True, text=True, check=True)
    found = defaultdict(set)
    for item in json.loads(result.stdout):
        path = Path(item["filename"]).resolve().relative_to(Path(root).resolve()).as_posix()
        found[path].add(item["code"])
    return dict(found)


def stale(entries, violations):
    """Baselined (path, code) pairs that the code no longer breaks."""
    return sorted(f"{path}: {code}" for path, codes in entries.items()
                  for code in codes - violations.get(path, set()))


def write(text, entries):
    head, rest = text.split(START, 1)
    block, tail = rest.split(END, 1)
    comments = [line for line in block.splitlines() if line.startswith("#")]
    return head + "\n".join([START, *comments, *([render(entries)] if entries else []), END]) + tail


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)
    text = PYPROJECT.read_text(encoding="utf-8")
    tomllib.loads(text)  # Refuse to touch a malformed file.
    violations = current_violations()
    if args.write:
        entries = {path: codes - (set() if path.startswith("tests/") else NEVER_BASELINED)
                   for path, codes in violations.items()}
        entries = {path: codes for path, codes in entries.items() if codes}
        PYPROJECT.write_text(write(text, entries), encoding="utf-8", newline="\n")
        print(f"ruff baseline: {len(entries)} files, {sum(map(len, entries.values()))} file/rule pairs")
        return 0
    entries = baseline(text)
    problems = [f"forbidden baseline entry {item}" for item in forbidden(entries)]
    problems += [f"stale baseline entry {item}; run --write" for item in stale(entries, violations)]
    for problem in problems:
        print("ruff baseline: " + problem, file=sys.stderr)
    if not problems:
        print(f"ruff baseline: {len(entries)} files, {sum(map(len, entries.values()))} file/rule pairs, none stale")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
