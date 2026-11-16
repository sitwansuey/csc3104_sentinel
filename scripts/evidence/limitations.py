"""Validate and render the known-limitations and defects register (docs/limitations.json).

Each entry cites evidence that must resolve on this tree: a file, optionally a
line or line range that still contains a quoted snippet, or a claim id from
evidence/claims.json. Structural problems always fail. Citations that do not
resolve are warnings by default so a draft can be edited; --strict, which CI
runs, makes them failures. docs/limitations.md and the README summary are
rendered from the register by scripts/evidence/summarize.py.
"""

import argparse
from datetime import date
import json
from pathlib import Path, PurePosixPath
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
REGISTER = "docs/limitations.json"
SCHEMA = "sentinel-limitations-v1"
KINDS = ("defect", "limitation")
STATUSES = ("open", "mitigated", "fixed")
FIELDS = ("id", "title", "kind", "status", "since", "impact", "workaround", "cites", "planned", "issues")
OPTIONAL = ("fixed_by",)
ENTRY_ID = re.compile(r"^L-[A-Z0-9]+(?:-[A-Z0-9]+)*$")
PLAN_ITEM = re.compile(r"^P[1-9]\.[0-9]{2}$")
PULL = re.compile(r"^#[1-9][0-9]*$")
AT = re.compile(r"^(?P<path>[^:]+?)(?::(?P<start>[1-9][0-9]*)(?:-(?P<end>[1-9][0-9]*))?)?$")
NONE = "_No entries yet._"


class RegisterError(ValueError):
    """The register file is missing or is not valid JSON."""


def load(root=ROOT):
    path = Path(root) / REGISTER
    if not path.is_file():
        raise RegisterError(f"{REGISTER} is missing")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise RegisterError(f"{REGISTER} is not valid JSON: {error}") from error


def claim_ids(root):
    path = Path(root) / "evidence/claims.json"
    if not path.is_file():
        return set()
    return {claim.get("id") for claim in json.loads(path.read_text(encoding="utf-8")).get("claims", [])}


def resolve(citation, root, claims):
    """None when the citation resolves on this tree, otherwise the reason it does not."""
    if not isinstance(citation, dict) or len({"at", "claim"} & set(citation)) != 1 \
            or set(citation) - {"at", "claim", "text"} or ("claim" in citation and "text" in citation):
        return "a citation is {'at': 'path[:line[-line]]', 'text': ...} or {'claim': 'C-...'}"
    if "claim" in citation:
        return None if citation["claim"] in claims else f"claim {citation['claim']} is not in evidence/claims.json"
    match = AT.match(str(citation["at"]))
    if not match:
        return f"cannot parse location {citation['at']!r}"
    relative = PurePosixPath(match.group("path"))
    if relative.is_absolute() or ".." in relative.parts or "\\" in match.group("path"):
        return f"{citation['at']}: use a repository-relative path with forward slashes"
    path = Path(root) / relative
    if not path.is_file():
        return f"{citation['at']}: file does not exist"
    text = citation.get("text")
    if match.group("start") and not (isinstance(text, str) and text.strip()):
        return f"{citation['at']}: a line citation needs the 'text' expected on that line"
    lines = path.read_text(encoding="utf-8").splitlines()
    if match.group("start"):
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if end < start or end > len(lines):
            return f"{citation['at']}: line range is outside the file ({len(lines)} lines)"
        cited = "\n".join(lines[start - 1:end])
    else:
        cited = "\n".join(lines)
    if text is not None and text not in cited:
        return f"{citation['at']}: does not contain {text!r}"
    return None


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate(registry, root=ROOT, *, strict=False):
    """Return (errors, warnings). Unresolved citations are errors only when strict."""
    errors, warnings = [], []
    if not isinstance(registry, dict) or registry.get("schema") != SCHEMA:
        return [f"schema must be {SCHEMA}"], warnings
    entries = registry.get("entries")
    if not isinstance(entries, list) or not entries:
        return ["entries must be a non-empty list"], warnings
    claims, seen = claim_ids(root), set()
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry {position}: must be an object")
            continue
        label = entry.get("id") if isinstance(entry.get("id"), str) else f"entry {position}"
        problem = lambda text: errors.append(f"{label}: {text}")
        missing = [name for name in FIELDS if name not in entry]
        if missing:
            problem("missing " + ", ".join(missing))
            continue
        unknown = sorted(set(entry) - set(FIELDS) - set(OPTIONAL))
        if unknown:
            problem("unknown field " + ", ".join(unknown))
        if not ENTRY_ID.match(str(entry["id"])):
            problem("id must look like L-UPPER-CASE")
        elif entry["id"] in seen:
            problem("duplicate id")
        seen.add(entry["id"])
        for name in ("title", "impact", "workaround"):
            if not isinstance(entry[name], str) or not entry[name].strip():
                problem(f"{name} must be non-empty text")
        if entry["kind"] not in KINDS:
            problem(f"kind must be one of {', '.join(KINDS)}")
        if entry["status"] not in STATUSES:
            problem(f"status must be one of {', '.join(STATUSES)}")
        if not _date(entry["since"]):
            problem("since must be an ISO date (YYYY-MM-DD)")
        planned = entry["planned"]
        if not isinstance(planned, list) or not all(isinstance(item, str) and PLAN_ITEM.match(item)
                                                     for item in planned):
            problem("planned must list roadmap item ids such as P1.10")
        elif not planned and entry["status"] != "fixed":
            problem("an entry that is not fixed must name the roadmap item that addresses it")
        issues = entry["issues"]
        if not isinstance(issues, list) or not all(type(item) is int and item > 0 for item in issues):
            problem("issues must list GitHub issue numbers")
        if entry["status"] == "fixed":
            if not PULL.match(str(entry.get("fixed_by", ""))):
                problem("a fixed entry needs fixed_by naming the pull request, such as #89")
        elif "fixed_by" in entry:
            problem("fixed_by is only for fixed entries")
        cites = entry["cites"]
        if not isinstance(cites, list) or not cites:
            problem("cites must be a non-empty list")
            continue
        if entry["kind"] == "defect" and entry["status"] != "fixed" \
                and not any(isinstance(c, dict) and ":" in str(c.get("at", "")) for c in cites):
            problem("an unfixed defect needs at least one file:line citation")
        for citation in cites:
            reason = resolve(citation, root, claims)
            if reason:
                (errors if strict else warnings).append(f"{label}: {reason}")
    return errors, warnings


# ----- rendering, called by summarize.py -----

ORDER = {("defect", "open"): 0, ("defect", "mitigated"): 1, ("limitation", "open"): 2,
         ("limitation", "mitigated"): 3, ("defect", "fixed"): 4, ("limitation", "fixed"): 5}


def ordered(entries):
    return sorted(entries, key=lambda entry: ORDER.get((entry["kind"], entry["status"]), 9))


def anchor(entry):
    return entry["id"].lower()


def _issues(entry):
    return ", ".join(f"#{number}" for number in entry["issues"]) or "none"


def _citation(citation):
    return f"claim `{citation['claim']}`" if "claim" in citation else f"`{citation['at']}`"


def render(registry):
    """The docs/limitations.md block: a summary table, then one section per entry."""
    entries = ordered(registry.get("entries", [])) if isinstance(registry, dict) else []
    if not entries:
        return NONE
    escape = lambda cell: str(cell).replace("|", "\\|")
    lines = ["| Entry | Kind | Status | Title | Planned | Issues |", "| --- | --- | --- | --- | --- | --- |"]
    for entry in entries:
        lines.append(f"| [{entry['id']}](#{anchor(entry)}) | {entry['kind']} | {entry['status']} | "
                     f"{escape(entry['title'])} | {', '.join(entry['planned']) or 'none'} | {_issues(entry)} |")
    for entry in entries:
        status = entry["status"] + (f" by {entry['fixed_by']}" if entry["status"] == "fixed" else "")
        lines += ["", f"### {entry['id']}", "",
                  f"**{entry['title']}.** {entry['kind'].capitalize()}, {status}; recorded {entry['since']}. "
                  f"Planned: {', '.join(entry['planned']) or 'none'}. Issues: {_issues(entry)}.", "",
                  f"- Impact: {entry['impact']}",
                  f"- Workaround: {entry['workaround']}",
                  "- Evidence: " + "; ".join(_citation(c) for c in entry["cites"]) + "."]
    return "\n".join(lines)


def render_summary(registry):
    """The README block: one line per entry that is not fixed."""
    entries = ordered(registry.get("entries", [])) if isinstance(registry, dict) else []
    current = [entry for entry in entries if entry["status"] != "fixed"]
    if not current:
        return NONE
    lines = [f"- [{entry['id']}](docs/limitations.md#{anchor(entry)}): {entry['title']} "
             f"({entry['kind']}, {entry['status']}; planned {', '.join(entry['planned'])})" for entry in current]
    fixed = len(entries) - len(current)
    if fixed:
        lines += ["", f"{fixed} fixed entries stay in the register as history."]
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--strict", action="store_true", help="Fail when any citation does not resolve")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        registry = load(args.root)
    except RegisterError as error:
        print("FAIL: " + str(error))
        return 1
    errors, warnings = validate(registry, args.root, strict=args.strict)
    for warning in warnings:
        print("WARN: " + warning)
    for error in errors:
        print("FAIL: " + error)
    if not errors:
        count = len(registry["entries"])
        print(f"{count} register entries are valid" + (" and every citation resolves" if args.strict else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
