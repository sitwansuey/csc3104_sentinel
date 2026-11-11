"""Block new hand-written copies of the canonical JSON encoding.

Digests bind Guard, Executor and Coordinator, so there must be one encoder:
shared/canonical.py. Any json.dump/json.dumps/JSONEncoder call with sort_keys
and compact separators elsewhere in product code is a copy. Import
shared.canonical instead.
"""

import ast
from pathlib import Path
import shutil
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("services", "runtime", "shared", "agent", "scripts", "experiments")
ALLOWED = {"shared/canonical.py"}
ENCODERS = {"dump", "dumps", "JSONEncoder"}


def _is_true(node):
    return isinstance(node, ast.Constant) and node.value is True


def _is_compact(node):
    return (isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) == 2
            and all(isinstance(item, ast.Constant) and isinstance(item.value, str) for item in node.elts)
            and [item.value.strip() for item in node.elts] == [",", ":"])


def _encoder_name(node):
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


def canonical_copies(root, directories=SCANNED):
    """Return 'path:line' for each canonical serialiser outside shared/canonical.py."""
    found = []
    for directory in directories:
        for path in sorted((Path(root) / directory).rglob("*.py")):
            relative = path.relative_to(root).as_posix()
            if relative in ALLOWED:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=relative)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or _encoder_name(node.func) not in ENCODERS:
                    continue
                keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
                if _is_true(keywords.get("sort_keys")) and _is_compact(keywords.get("separators")):
                    found.append(f"{relative}:{node.lineno}")
    return found


class NoDuplicateCanonicalTests(unittest.TestCase):
    def plant(self, files):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for relative, source in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
        return root

    def test_repository_has_no_copy_outside_shared_canonical(self):
        self.assertEqual(canonical_copies(ROOT), [])

    def test_shared_canonical_itself_is_the_encoder(self):
        source = (ROOT / "shared/canonical.py").read_text(encoding="utf-8")
        self.assertIn('separators=(",", ":")', source)
        self.assertIn("sort_keys=True", source)

    def test_planted_copies_are_caught(self):
        root = self.plant({
            "services/planted/store.py": """
                import json
                def _json(value):
                    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            """,
            "runtime/planted.py": """
                from json import dumps
                def encoded(value):
                    return dumps(value, separators=[",", ":"], sort_keys=True)
            """,
            "scripts/planted.py": """
                import json
                def write(value, stream):
                    json.dump(value, stream, sort_keys=True, separators=(', ', ': '))
            """,
            "agent/planted.py": """
                import json
                ENCODER = json.JSONEncoder(sort_keys=True, separators=(",", ":"))
            """,
        })
        self.assertEqual(sorted(canonical_copies(root)), sorted([
            "services/planted/store.py:4", "runtime/planted.py:4",
            "scripts/planted.py:4", "agent/planted.py:3",
        ]))

    def test_allowed_module_and_non_canonical_calls_are_ignored(self):
        root = self.plant({
            "shared/canonical.py": """
                import json
                def canonical_json(value):
                    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            """,
            "services/display.py": """
                import json
                def show(value):
                    print(json.dumps(value, sort_keys=True))
                    print(json.dumps(value, separators=(",", ":")))
                    print(json.dumps(value, sort_keys=False, separators=(",", ":")))
                    print(json.dumps(value, indent=2, sort_keys=True))
            """,
            "tests/test_planted.py": """
                import json
                json.dumps({}, sort_keys=True, separators=(",", ":"))
            """,
        })
        self.assertEqual(canonical_copies(root), [])

    def test_unparsable_source_fails_loudly(self):
        root = self.plant({"services/broken.py": "def broken(:\n"})
        with self.assertRaises(SyntaxError):
            canonical_copies(root)


if __name__ == "__main__":
    unittest.main()
