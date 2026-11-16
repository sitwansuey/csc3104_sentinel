"""Known-limitations register: every entry is well formed and its citations resolve on this tree."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evidence import limitations, summarize

ROOT = Path(__file__).resolve().parents[1]

ENTRY = {
    "id": "L-EXAMPLE",
    "title": "Example defect",
    "kind": "defect",
    "status": "open",
    "since": "2026-11-11",
    "impact": "Something goes wrong.",
    "workaround": "None.",
    "cites": [{"at": "src/module.py:2", "text": "risky()"}, {"claim": "C-ONE"}],
    "planned": ["P1.10"],
    "issues": [61],
}


class RepositoryRegisterTests(unittest.TestCase):
    def setUp(self):
        self.registry = limitations.load(ROOT)

    def test_register_is_valid_and_every_citation_resolves(self):
        self.assertEqual(limitations.validate(self.registry, ROOT, strict=True), ([], []))

    def test_register_covers_the_known_evaluation_defects(self):
        ids = {entry["id"] for entry in self.registry["entries"]}
        for expected in ("L-SINGLE-COORDINATOR", "L-CLOCK-ORDERING", "L-HOST-LOCAL-EXECUTOR", "L-NO-FENCING",
                         "L-AGENT-JOURNAL", "L-AGENT-APPROVAL", "L-RAW-LOGS-PROMPT", "L-ONE-FAULT-ONE-ACTION",
                         "L-SECURITY-SHIPPING-ONLY", "L-UNGATED-CVES", "L-EXEC-WEDGE", "L-NO-LOGGING"):
            self.assertIn(expected, ids)

    def test_every_unfixed_entry_names_a_roadmap_item(self):
        for entry in self.registry["entries"]:
            if entry["status"] != "fixed":
                with self.subTest(entry=entry["id"]):
                    self.assertTrue(entry["planned"])

    def test_generated_pages_are_current(self):
        out = io.StringIO()
        self.assertEqual(summarize.run(ROOT, check=True, out=out), 0, out.getvalue())

    def test_ci_runs_the_strict_validator(self):
        workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")
        self.assertIn("python scripts/evidence/limitations.py --strict", workflow)


class RegisterValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src/module.py").write_text("def handler():\n    risky()\n    return 1\n", encoding="utf-8")
        (self.root / "evidence").mkdir()
        (self.root / "evidence/claims.json").write_text(json.dumps(
            {"schema": "sentinel-claims-v1", "claims": [{"id": "C-ONE"}]}), encoding="utf-8")
        self.reset()

    def tearDown(self):
        self.temp.cleanup()

    def reset(self):
        self.registry = {"schema": limitations.SCHEMA, "entries": [copy.deepcopy(ENTRY)]}

    def check(self, change=None, strict=True):
        if change:
            change(self.registry["entries"][0])
        return limitations.validate(self.registry, self.root, strict=strict)

    def errors(self, change=None, strict=True):
        return self.check(change, strict)[0]

    def test_valid_entry_passes_strict(self):
        self.assertEqual(self.check(), ([], []))

    def test_renamed_file_fails_strict_and_only_warns_without_it(self):
        (self.root / "src/module.py").rename(self.root / "src/renamed.py")
        self.assertEqual(self.errors(), ["L-EXAMPLE: src/module.py:2: file does not exist"])
        errors, warnings = self.check(strict=False)
        self.assertEqual(errors, [])
        self.assertEqual(warnings, ["L-EXAMPLE: src/module.py:2: file does not exist"])

    def test_line_beyond_the_file_fails(self):
        problems = self.errors(lambda e: e["cites"][0].update(at="src/module.py:9"))
        self.assertEqual(problems, ["L-EXAMPLE: src/module.py:9: line range is outside the file (3 lines)"])

    def test_inverted_range_fails(self):
        problems = self.errors(lambda e: e["cites"][0].update(at="src/module.py:3-2"))
        self.assertIn("line range is outside the file", problems[0])

    def test_drifted_line_fails_even_when_still_in_range(self):
        (self.root / "src/module.py").write_text("def handler():\n    return 1\n    risky()\n", encoding="utf-8")
        self.assertEqual(self.errors(), ["L-EXAMPLE: src/module.py:2: does not contain 'risky()'"])

    def test_line_range_containing_the_text_passes(self):
        self.assertEqual(self.errors(lambda e: e["cites"][0].update(at="src/module.py:1-3")), [])

    def test_line_citation_needs_text(self):
        problems = self.errors(lambda e: e["cites"][0].pop("text"))
        self.assertEqual(problems, ["L-EXAMPLE: src/module.py:2: a line citation needs the 'text' expected on "
                                    "that line"])

    def test_file_citation_without_line_passes_and_checks_optional_text(self):
        self.assertEqual(self.errors(lambda e: e["cites"].append({"at": "src/module.py"})), [])
        problems = self.errors(lambda e: e["cites"].append({"at": "src/module.py", "text": "absent"}))
        self.assertEqual(problems, ["L-EXAMPLE: src/module.py: does not contain 'absent'"])

    def test_unknown_claim_fails(self):
        problems = self.errors(lambda e: e["cites"][1].update(claim="C-MISSING"))
        self.assertEqual(problems, ["L-EXAMPLE: claim C-MISSING is not in evidence/claims.json"])

    def test_paths_outside_the_repository_fail(self):
        for location in ("../outside.py:1", "/etc/passwd:1", "src\\module.py:2"):
            with self.subTest(location=location):
                self.registry["entries"][0]["cites"][0]["at"] = location
                self.assertIn("repository-relative path", self.errors()[0])

    def test_malformed_citation_fails(self):
        for citation in ({"claim": "C-ONE", "at": "src/module.py"}, {"file": "src/module.py"}, "src/module.py:2",
                         {"claim": "C-ONE", "text": "x"}):
            with self.subTest(citation=citation):
                self.registry["entries"][0]["cites"] = [{"at": "src/module.py:2", "text": "risky()"}, citation]
                self.assertEqual(len(self.errors()), 1)

    def test_kind_and_status_are_restricted(self):
        self.assertIn("L-EXAMPLE: kind must be one of defect, limitation", self.errors(lambda e: e.update(kind="bug")))
        self.assertIn("L-EXAMPLE: status must be one of open, mitigated, fixed",
                      self.errors(lambda e: e.update(status="done")))

    def test_duplicate_ids_fail(self):
        self.registry["entries"].append(copy.deepcopy(ENTRY))
        self.assertEqual(self.errors(), ["L-EXAMPLE: duplicate id"])

    def test_missing_and_unknown_fields_fail(self):
        self.assertEqual(self.errors(lambda e: e.pop("impact")), ["L-EXAMPLE: missing impact"])
        self.reset()
        self.assertEqual(self.errors(lambda e: e.update(cite=[])), ["L-EXAMPLE: unknown field cite"])

    def test_unfixed_entry_needs_a_well_formed_roadmap_item(self):
        self.assertEqual(self.errors(lambda e: e.update(planned=[])),
                         ["L-EXAMPLE: an entry that is not fixed must name the roadmap item that addresses it"])
        self.assertEqual(self.errors(lambda e: e.update(planned=["1.10"])),
                         ["L-EXAMPLE: planned must list roadmap item ids such as P1.10"])

    def test_fixed_entry_needs_its_pull_request_and_open_entry_must_not_have_one(self):
        self.assertEqual(self.errors(lambda e: e.update(status="fixed")),
                         ["L-EXAMPLE: a fixed entry needs fixed_by naming the pull request, such as #89"])
        self.assertEqual(self.errors(lambda e: e.update(status="fixed", fixed_by="#93", planned=[])), [])
        self.reset()
        self.assertEqual(self.errors(lambda e: e.update(fixed_by="#93")),
                         ["L-EXAMPLE: fixed_by is only for fixed entries"])

    def test_unfixed_defect_needs_a_code_line(self):
        problems = self.errors(lambda e: e.update(cites=[{"claim": "C-ONE"}]))
        self.assertEqual(problems, ["L-EXAMPLE: an unfixed defect needs at least one file:line citation"])
        self.assertEqual(self.errors(lambda e: e.update(kind="limitation")), [])

    def test_bad_id_issue_and_date_fail(self):
        problems = self.errors(lambda e: e.update(id="example", issues=["#61"], since="11/11/2026"))
        self.assertEqual(problems, ["example: id must look like L-UPPER-CASE",
                                    "example: since must be an ISO date (YYYY-MM-DD)",
                                    "example: issues must list GitHub issue numbers"])

    def test_wrong_schema_or_empty_register_fails(self):
        self.assertEqual(limitations.validate({"schema": "other", "entries": []}, self.root)[0],
                         [f"schema must be {limitations.SCHEMA}"])
        self.assertEqual(limitations.validate({"schema": limitations.SCHEMA, "entries": []}, self.root)[0],
                         ["entries must be a non-empty list"])

    def test_command_exit_codes(self):
        (self.root / "docs").mkdir()
        path = self.root / limitations.REGISTER
        self.registry["entries"][0]["cites"][0]["at"] = "src/gone.py:2"
        path.write_text(json.dumps(self.registry), encoding="utf-8")
        for strict, code, prefix in ((True, 1, "FAIL: "), (False, 0, "WARN: ")):
            with self.subTest(strict=strict):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    argv = ["--root", str(self.root)] + (["--strict"] if strict else [])
                    self.assertEqual(limitations.main(argv), code)
                self.assertIn(prefix + "L-EXAMPLE: src/gone.py:2: file does not exist", out.getvalue())
        path.write_text("{not json", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(limitations.main(["--root", str(self.root), "--strict"]), 1)


class RenderTests(unittest.TestCase):
    def registry(self):
        fixed = dict(copy.deepcopy(ENTRY), id="L-OLD", title="Old defect", status="fixed", fixed_by="#89",
                     planned=[])
        limitation = dict(copy.deepcopy(ENTRY), id="L-SCOPE", title="Narrow scope", kind="limitation",
                          planned=["P2.08", "P3.08"], issues=[])
        return {"schema": limitations.SCHEMA, "entries": [fixed, limitation, copy.deepcopy(ENTRY)]}

    def test_page_lists_open_defects_first_with_anchored_sections(self):
        text = limitations.render(self.registry())
        rows = [line for line in text.splitlines() if line.startswith("| [")]
        self.assertEqual([row.split("]")[0][3:] for row in rows], ["L-EXAMPLE", "L-SCOPE", "L-OLD"])
        self.assertIn("| [L-EXAMPLE](#l-example) | defect | open | Example defect | P1.10 | #61 |", text)
        self.assertIn("### L-OLD", text)
        self.assertIn("Defect, fixed by #89; recorded 2026-11-11. Planned: none. Issues: #61.", text)
        self.assertIn("- Evidence: `src/module.py:2`; claim `C-ONE`.", text)

    def test_readme_summary_omits_fixed_entries_but_counts_them(self):
        text = limitations.render_summary(self.registry())
        self.assertIn("- [L-SCOPE](docs/limitations.md#l-scope): Narrow scope (limitation, open; planned P2.08, P3.08)",
                      text)
        self.assertNotIn("L-OLD", text)
        self.assertIn("1 fixed entries stay in the register as history.", text)

    def test_empty_register_renders_a_placeholder(self):
        self.assertEqual(limitations.render({}), limitations.NONE)
        self.assertEqual(limitations.render_summary({"entries": []}), limitations.NONE)

    def test_malformed_register_stops_summarize(self):
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "docs").mkdir()
            (Path(temp) / limitations.REGISTER).write_text(json.dumps({"schema": limitations.SCHEMA, "entries": [
                dict(ENTRY, kind="bug")]}), encoding="utf-8")
            with self.assertRaises(summarize.EvidenceError):
                summarize.limitation_blocks(temp)


if __name__ == "__main__":
    unittest.main()
