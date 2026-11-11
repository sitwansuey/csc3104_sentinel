"""Audit broad exception handlers: none may swallow an error silently.

A handler is broad when it is a bare 'except:' or catches Exception or
BaseException (alone or in a tuple). Each one must either:

- re-raise (a bare 'raise', 'raise <name>' or 'raise ... from <name>'), or
- carry '# noqa: BLE001 - <reason>' on its except line and log through
  shared/log.py (log.failure(), log.emit() or access_log()), so the class name
  of the cause is recorded.

Bare 'except:' is never accepted. Ruff's BLE001 and S110 rules stop new silent
broad handlers in CI; this script also checks that every suppression gives a
reason and logs.

    python scripts/ci/except_audit.py            # fail on an unjustified handler
    python scripts/ci/except_audit.py --report   # also print counts per category
"""
import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
DIRECTORIES = ("services", "runtime", "shared", "agent", "experiments", "scripts")
BROAD = frozenset({"Exception", "BaseException"})
LOG_CALLS = frozenset({"failure", "emit", "access_log"})
NOQA = re.compile(r"#\s*noqa:\s*BLE001\b\s*(?:[-:]+\s*)?(?P<reason>.*)$")
MIN_REASON_WORDS = 3

# Handlers that log after the try statement rather than inside the handler.
# Keyed by (path, enclosing function); the value says where the log line is.
LOGGED_AFTER = {
    ("runtime/worker_http.py", "do_POST"): "access_log() after the try writes error_type",
    ("runtime/approval_http.py", "do_POST"): "access_log() after the try writes error_type",
    ("runtime/worker_service.py", "main"): "report() after the try writes worker.status with error_type",
}
# Files that cannot import shared/log.py, with the reason.
EXEMPT = {
    "experiments/boutique/traffic/worker.py":
        "runs in a cluster pod from a ConfigMap holding only its own two files; "
        "each JSONL row records the error class",
}


@dataclass(frozen=True)
class Handler:
    path: str
    line: int
    caught: str
    function: str
    reraises: bool
    logs: bool
    reason: str | None


def _caught(node):
    if node.type is None:
        return "bare"
    names = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
    broad = [name.id for name in names if isinstance(name, ast.Name) and name.id in BROAD]
    return broad[0] if broad else None


def _walk(nodes):
    """Statements and expressions of a handler body, not of nested handlers or definitions."""
    pending = list(nodes)
    while pending:
        node = pending.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.ExceptHandler, ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.Lambda, ast.ClassDef)):
                pending.append(child)


def _reraises(handler):
    for node in _walk(handler.body):
        if isinstance(node, ast.Raise):
            if node.exc is None:
                return True
            if handler.name and any(isinstance(item, ast.Name) and item.id == handler.name
                                    for item in (node.exc, node.cause)):
                return True
    return False


def _logs(handler):
    for node in _walk(handler.body):
        if isinstance(node, ast.Call):
            function = node.func
            name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
            if name in LOG_CALLS:
                return True
    return False


def handlers(path, root=ROOT, source=None):
    """Every broad handler in one file."""
    path = Path(path)
    text = path.read_text(encoding="utf-8") if source is None else source
    lines = text.splitlines()
    tree = ast.parse(text, filename=str(path))
    relative = path.resolve().relative_to(Path(root).resolve()).as_posix() if source is None else str(path)
    found = []

    def visit(node, function):
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
            if isinstance(child, ast.ExceptHandler):
                caught = _caught(child)
                if caught is not None:
                    noqa = NOQA.search(lines[child.lineno - 1])
                    found.append(Handler(relative, child.lineno, caught, function, _reraises(child),
                                         _logs(child), noqa.group("reason").strip() if noqa else None))
            visit(child, name)

    visit(tree, "<module>")
    return found


def scan(root=ROOT, directories=DIRECTORIES):
    found = []
    for directory in directories:
        for path in sorted(Path(root, directory).rglob("*.py")):
            found.extend(handlers(path, root))
    return found


def problems(found):
    result = []
    for item in found:
        where = f"{item.path}:{item.line}"
        if item.caught == "bare":
            result.append(f"{where}: bare 'except:'; catch Exception at most, and justify it")
        elif item.reraises:
            continue
        elif item.reason is None:
            result.append(f"{where}: broad 'except {item.caught}' without '# noqa: BLE001 - <reason>'")
        elif len(item.reason.split()) < MIN_REASON_WORDS:
            result.append(f"{where}: the noqa reason must say why the handler is broad")
        elif not (item.logs or (item.path, item.function) in LOGGED_AFTER or item.path in EXEMPT):
            result.append(f"{where}: broad handler does not log the cause through shared/log.py")
    return result


def summary(found):
    counts = {"total": len(found), "reraise": 0, "logged": 0, "logged_after": 0, "exempt": 0, "unjustified": 0}
    flagged = {line.split(": ", 1)[0] for line in problems(found)}
    for item in found:
        if f"{item.path}:{item.line}" in flagged:
            counts["unjustified"] += 1
        elif item.reraises:
            counts["reraise"] += 1
        elif item.logs:
            counts["logged"] += 1
        elif item.path in EXEMPT:
            counts["exempt"] += 1
        else:
            counts["logged_after"] += 1
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--report", action="store_true", help="Print counts per category")
    args = parser.parse_args(argv)
    found = scan()
    issues = problems(found)
    for issue in issues:
        print("except-audit: " + issue, file=sys.stderr)
    if args.report:
        print("except-audit: " + ", ".join(f"{key}={value}" for key, value in summary(found).items()))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
