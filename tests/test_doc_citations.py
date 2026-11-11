"""Code citations in the threat-model and distributed-model docs must resolve."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import check_doc_citations

ROOT = Path(__file__).resolve().parents[1]

MODULE = "\n".join([
    "LIMIT = 3",                      # 1
    "",                               # 2
    "class Store:",                   # 3
    "    def save(self, value):",     # 4
    "        checked = value > 0",    # 5
    "        return checked",         # 6
    "",                               # 7
    "def handle(message):",           # 8
    "    def inner():",               # 9
    "        return message",         # 10
    "    return inner()",             # 11
    "",
])
MANIFEST = "kind: Deployment\nspec:\n  replicas: 1\n  strategy:\n    type: Recreate\n"
TESTS = "import unittest\n\nclass StoreTests(unittest.TestCase):\n    def test_save(self):\n        pass\n"
CLAIMS = {"schema": "sentinel-claims-v1", "claims": [{"id": "C-KNOWN-CLAIM"}]}


def tree(document, *, module=MODULE):
    temp = tempfile.TemporaryDirectory()
    root = Path(temp.name)
    files = {"pkg/store.py": module, "deploy/app.yaml": MANIFEST, "tests/test_store.py": TESTS,
             "evidence/claims.json": json.dumps(CLAIMS), "docs/model.md": document}
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return temp


def problems(document, *, module=MODULE):
    with tree(document, module=module) as temp:
        return check_doc_citations.check(temp, ("docs/model.md",))


class CitationParsingTests(unittest.TestCase):
    def test_paths_lines_symbols_and_claims_are_recognised(self):
        text = "\n".join([
            "`pkg/store.py` `pkg/store.py:5` `pkg/store.py:4-6::Store.save` `README.md` `C-KNOWN-CLAIM`",
            "`deploy/` `deploy/app.yaml:5::Recreate` `tests/test_store.py::StoreTests::test_save`",
        ])
        self.assertEqual([span for span, _ in check_doc_citations.citations(text)], [
            "pkg/store.py", "pkg/store.py:5", "pkg/store.py:4-6::Store.save", "README.md", "C-KNOWN-CLAIM",
            "deploy/", "deploy/app.yaml:5::Recreate", "tests/test_store.py::StoreTests::test_save"])

    def test_prose_spans_urls_and_fenced_blocks_are_not_citations(self):
        text = "\n".join([
            "`BEGIN IMMEDIATE` `approve()` `python:3.12-slim` `/api/lab/inject` `actions/checkout@v6`",
            "`ValueError` `1.0` `synchronous=FULL` `SQLiteStore.__init__`",
            "```mermaid", "A[`pkg/missing.py`] --> B", "```",
        ])
        self.assertEqual(check_doc_citations.citations(text), [])

    def test_symbol_table_covers_nested_definitions_and_module_assignments(self):
        table = check_doc_citations.symbols(MODULE)
        self.assertEqual(table["LIMIT"], (1, 1))
        self.assertEqual(table["Store.save"], (4, 6))
        self.assertEqual(table["handle.inner"], (9, 10))


class CitationCheckTests(unittest.TestCase):
    def test_resolving_citations_pass(self):
        self.assertEqual(problems(
            "`pkg/store.py` `pkg/store.py::Store.save` `pkg/store.py:5::Store.save` `pkg/store.py:8-11::handle` "
            "`pkg/store.py::LIMIT` `deploy/app.yaml:3` `deploy/app.yaml:4-5::type: Recreate` `deploy/app.yaml::replicas` "
            "`tests/test_store.py::StoreTests.test_save` `tests/test_store.py::StoreTests::test_save` "
            "`deploy/` `C-KNOWN-CLAIM`"), [])

    def test_each_broken_citation_is_reported(self):
        cases = {
            "`pkg/missing.py`": "missing file",
            "`pkg/store.py:12`": "lines 12-12 outside 1-11",
            "`pkg/store.py:6-4::Store.save`": "lines 6-4 outside 1-11",
            "`pkg/store.py:5`": "line citation into Python needs a ::symbol anchor",
            "`pkg/store.py::Store.load`": "symbol Store.load not found",
            "`pkg/store.py:8::Store.save`": "lines 8-8 are not inside Store.save (lines 4-6)",
            "`deploy/app.yaml:3::Recreate`": "text 'Recreate' not found on the cited lines",
            "`deploy/app.yaml::RollingUpdate`": "text 'RollingUpdate' not found",
            "`deploy/:2`": "lines or symbols need a file",
            "`tests/test_store.py::StoreTests.test_load`": "symbol StoreTests.test_load not found",
            "`C-UNKNOWN-CLAIM`": "unknown claim id",
            "`../outside.md`": "path leaves the repository",
        }
        for citation, reason in cases.items():
            with self.subTest(citation=citation):
                self.assertEqual(problems(citation), [f"docs/model.md: {citation} ({reason})"])

    def test_renamed_symbol_in_a_fixture_copy_fails(self):
        document = "`pkg/store.py:4-6::Store.save`"
        self.assertEqual(problems(document), [])
        renamed = MODULE.replace("def save(", "def persist(")
        self.assertEqual(problems(document, module=renamed),
                         ["docs/model.md: `pkg/store.py:4-6::Store.save` (symbol Store.save not found)"])

    def test_code_moving_out_of_the_cited_function_fails(self):
        document = "`pkg/store.py:5::Store.save`"
        moved = MODULE.replace("class Store:", "def helper():\n    return 1\n\nclass Store:")
        self.assertEqual(problems(document, module=moved),
                         ["docs/model.md: `pkg/store.py:5::Store.save` (lines 5-5 are not inside Store.save (lines 7-9))"])

    def test_unparseable_python_and_missing_documents_fail(self):
        self.assertEqual(problems("`pkg/store.py::Store`", module="def broken(:\n"),
                         ["docs/model.md: `pkg/store.py::Store` (file does not parse)"])
        with tree("`pkg/store.py`") as temp:
            self.assertEqual(check_doc_citations.check(temp, ("docs/absent.md",)),
                             ["docs/absent.md: document is missing"])

    def test_a_document_without_citations_fails(self):
        self.assertEqual(problems("No code is cited here."), ["docs/model.md: no citations found"])

    def test_main_exits_non_zero_and_names_the_citation(self):
        output = io.StringIO()
        with tree("`pkg/missing.py`") as temp, contextlib.redirect_stdout(output):
            self.assertEqual(check_doc_citations.main(["--root", temp, "docs/model.md"]), 1)
        self.assertIn("FAIL: docs/model.md: `pkg/missing.py` (missing file)", output.getvalue())

    def test_repository_model_documents_resolve(self):
        self.assertEqual(check_doc_citations.check(ROOT), [])
        for name in check_doc_citations.DOCUMENTS:
            with self.subTest(document=name):
                self.assertGreater(len(check_doc_citations.citations((ROOT / name).read_text(encoding="utf-8"))), 20)


if __name__ == "__main__":
    unittest.main()
