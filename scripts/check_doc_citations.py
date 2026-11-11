"""Fail when a code citation in the threat-model or distributed-model docs stops resolving.

A citation is an inline code span (backticks) outside fenced blocks:

  `path`                 the file or directory exists
  `path:N`, `path:N-M`   the file exists and has those lines
  `path::Name`           Python: a class, function, method (Class.method) or
                         module-level assignment found with ast; other files:
                         the text appears in the file
  `path:N-M::Name::text` Python: lines N-M lie inside Name's definition, and
                         text is on line N and on no other line of Name
  `path:N-M::text`       other files: lines N-M contain the text
  `C-SOME-CLAIM`         a claim id in evidence/claims.json

Paths are relative to the repository root. A span is treated as a path when it
contains '/' or ends in a known file extension; other spans (prose such as
`approve()` or `SQLiteStore.__init__`) are not checked. A line citation into
a Python file must name its enclosing symbol and a text that occurs once in
it, on the first cited line. Code that moves to another function, or a line
added or removed above the cited line, then fails the check. Lines added or
removed inside a cited range after its first line are not detected, and the
check cannot tell whether the cited code still does what the prose says.
Standard library only, offline.
"""

import argparse
import ast
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = ("docs/threat-model.md", "docs/distributed-model.md")
EXTENSIONS = frozenset({"py", "md", "yaml", "yml", "json", "ts", "tsx", "sh", "ps1", "toml", "txt", "Dockerfile"})
FENCE = re.compile(r"^\s{0,3}(```|~~~).*?^\s{0,3}\1[^\n]*$", re.MULTILINE | re.DOTALL)
SPAN = re.compile(r"`([^`\n]+)`")
CITATION = re.compile(r"^(?P<path>[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)*/?)"
                      r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?(?:::(?P<anchor>\S.*))?$")
CLAIM = re.compile(r"^C-[A-Z0-9]+(?:-[A-Z0-9]+)*$")


def _is_path(path):
    return "/" in path or path.rsplit(".", 1)[-1] in EXTENSIONS and "." in path


def citations(text):
    """Return (span, match or None) for every citation-like inline code span."""
    found = []
    for span in SPAN.findall(FENCE.sub("", text)):
        span = span.strip()
        if CLAIM.match(span):
            found.append((span, None))
            continue
        match = CITATION.match(span)
        if match and _is_path(match.group("path")):
            found.append((span, match))
    return found


def symbols(source):
    """Map dotted names to (first line, last line) for a Python module."""
    table = {}

    def visit(body, prefix):
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                first = min([node.lineno] + [item.lineno for item in node.decorator_list])
                table[name] = (first, node.end_lineno)
                visit(node.body, name + ".")
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name):
                        table[prefix + target.id] = (node.lineno, node.end_lineno)

    visit(ast.parse(source).body, "")
    return table


class Resolver:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self._text, self._symbols, self._claims = {}, {}, None

    def text(self, path):
        if path not in self._text:
            self._text[path] = path.read_text(encoding="utf-8")
        return self._text[path]

    def symbol_table(self, path):
        if path not in self._symbols:
            self._symbols[path] = symbols(self.text(path))
        return self._symbols[path]

    def claims(self):
        if self._claims is None:
            try:
                data = json.loads((self.root / "evidence" / "claims.json").read_text(encoding="utf-8"))
                self._claims = {claim["id"] for claim in data["claims"]}
            except (OSError, ValueError, KeyError, TypeError):
                self._claims = set()
        return self._claims

    def problem(self, span, match):
        """Return why a citation does not resolve, or None."""
        if match is None:
            return None if span in self.claims() else "unknown claim id"
        relative = match.group("path")
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            return "path leaves the repository"
        if not path.exists():
            return "missing file"
        start, end, anchor = match.group("start"), match.group("end"), match.group("anchor")
        if (start or anchor) and not path.is_file():
            return "lines or symbols need a file"
        lines = None
        if start:
            first, last = int(start), int(end or start)
            count = len(self.text(path).splitlines())
            if not 1 <= first <= last <= count:
                return f"lines {first}-{last} outside 1-{count}"
            lines = (first, last)
        if path.suffix == ".py" and (lines or anchor):
            parts, guard = (anchor or "").split("::"), None
            if lines:
                if len(parts) < 2 or not all(parts[:-1]) or not parts[-1].strip():
                    return "line citation into Python needs ::symbol::text"
                parts, guard = parts[:-1], parts[-1]
            try:
                table = self.symbol_table(path)
            except SyntaxError:
                return "file does not parse"
            name = ".".join(parts)
            if name not in table:
                return f"symbol {name} not found"
            if lines and not (table[name][0] <= lines[0] and lines[1] <= table[name][1]):
                return f"lines {lines[0]}-{lines[1]} are not inside {name} (lines {table[name][0]}-{table[name][1]})"
            if guard is not None:
                source = self.text(path).splitlines()
                hits = [number for number in range(table[name][0], table[name][1] + 1)
                        if guard in source[number - 1]]
                if lines[0] not in hits:
                    return f"text {guard!r} is not on line {lines[0]}"
                if hits != [lines[0]]:
                    shown = ", ".join(str(number) for number in hits)
                    return f"text {guard!r} is on lines {shown} of {name}, not only line {lines[0]}"
        elif anchor:
            text = self.text(path)
            if lines:
                text = "\n".join(text.splitlines()[lines[0] - 1:lines[1]])
            if anchor not in text:
                return f"text {anchor!r} not found" + (" on the cited lines" if lines else "")
        return None


def check(root=ROOT, documents=DOCUMENTS):
    """Return 'doc: `citation` (reason)' strings for every citation that does not resolve."""
    resolver = Resolver(root)
    problems = []
    for name in documents:
        source = resolver.root / name
        if not source.is_file():
            problems.append(f"{name}: document is missing")
            continue
        found = citations(source.read_text(encoding="utf-8"))
        if not found:
            problems.append(f"{name}: no citations found")
        for span, match in found:
            reason = resolver.problem(span, match)
            if reason:
                problems.append(f"{name}: `{span}` ({reason})")
    return problems


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("documents", nargs="*", help="repository-relative Markdown files (default: the two model docs)")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    problems = check(args.root, tuple(args.documents) or DOCUMENTS)
    for problem in problems:
        print("FAIL: " + problem)
    if not problems:
        print("All code citations resolve")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
