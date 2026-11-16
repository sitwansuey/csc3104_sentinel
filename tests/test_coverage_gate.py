"""The coverage ratchet: per-package floors, failing closed on bad input."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.ci import coverage_gate

ROOT = Path(__file__).resolve().parents[1]


def report(**packages):
    """A 'coverage json' style report: one file per package with covered/total lines."""
    files = {}
    for index, (package, (covered, total)) in enumerate(packages.items()):
        name = package.replace("__", "/") + f"/module{index}.py"
        files[name] = {"summary": {"covered_lines": covered, "num_statements": total,
                                   "covered_branches": 0, "num_branches": 0}}
    return {"files": files}


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def run_gate(self, coverage, floors, *extra):
        report_path, floors_path = self.directory / "coverage.json", self.directory / "floors.json"
        report_path.write_text(json.dumps(coverage) if not isinstance(coverage, str) else coverage, encoding="utf-8")
        floors_path.write_text(json.dumps(floors) if not isinstance(floors, str) else floors, encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), patch.dict("os.environ", {}, clear=False) as env:
            env.pop("GITHUB_STEP_SUMMARY", None)
            code = coverage_gate.main([str(report_path), "--floors", str(floors_path), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_passes_when_every_package_meets_its_floor(self):
        code, out, _err = self.run_gate(report(services__executor=(95, 100), shared=(80, 100)),
                                        {"packages": {"total": 85, "services/executor": 95, "shared": 80}})
        self.assertEqual(code, 0)
        self.assertIn("| services/executor | 95.00% | 95.00% | pass |", out)
        self.assertIn("| total | 87.50% |", out)

    def test_fails_naming_the_package_that_dropped(self):
        code, out, err = self.run_gate(report(services__executor=(89, 100), shared=(99, 100)),
                                       {"packages": {"services/executor": 90, "shared": 95}})
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("services/executor", err)
        self.assertNotIn("shared", err)

    def test_branches_count_like_coverage_py(self):
        coverage = {"files": {"shared/a.py": {"summary": {"covered_lines": 10, "num_statements": 10,
                                                          "covered_branches": 0, "num_branches": 10}}}}
        self.assertEqual(self.run_gate(coverage, {"packages": {"shared": 50}})[0], 0)
        self.assertEqual(self.run_gate(coverage, {"packages": {"shared": 50.1}})[0], 1)

    def test_windows_paths_are_matched(self):
        coverage = {"files": {"services\\executor\\core.py": {"summary": {"covered_lines": 9, "num_statements": 10}}}}
        self.assertEqual(self.run_gate(coverage, {"packages": {"services/executor": 90}})[0], 0)

    def test_package_missing_from_the_report_fails_closed(self):
        code, out, err = self.run_gate(report(shared=(100, 100)), {"packages": {"shared": 90, "runtime": 10}})
        self.assertEqual(code, 1)
        self.assertIn("| runtime | missing |", out)
        self.assertIn("runtime", err)

    def test_prefix_does_not_match_a_sibling_package(self):
        # "services/executor" must not count "services/executor_extra/...".
        coverage = {"files": {"services/executor_extra/a.py": {"summary": {"covered_lines": 1, "num_statements": 1}}}}
        self.assertEqual(self.run_gate(coverage, {"packages": {"services/executor": 0}})[0], 1)

    def test_malformed_floors_fail_with_a_usage_error(self):
        for floors in ("{not json", {"packages": {}}, {"packages": {"shared": 101}},
                       {"packages": {"shared": "90"}}, {"packages": {"shared": True}}, ["shared"],
                       {"packages": {"C:\\shared": 90}}):
            with self.subTest(floors=floors):
                code, _out, err = self.run_gate(report(shared=(1, 1)), floors)
                self.assertEqual(code, 2)
                self.assertIn("coverage gate:", err)

    def test_malformed_report_fails_with_a_usage_error(self):
        for coverage in ("{", {"totals": {}}):
            with self.subTest(coverage=coverage):
                self.assertEqual(self.run_gate(coverage, {"packages": {"shared": 1}})[0], 2)

    def test_summary_table_is_written_for_the_ci_job(self):
        summary = self.directory / "summary.md"
        report_path, floors_path = self.directory / "c.json", self.directory / "f.json"
        report_path.write_text(json.dumps(report(shared=(1, 1))), encoding="utf-8")
        floors_path.write_text(json.dumps({"packages": {"shared": 90}}), encoding="utf-8")
        with redirect_stdout(io.StringIO()), patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}):
            self.assertEqual(coverage_gate.main([str(report_path), "--floors", str(floors_path)]), 0)
        self.assertIn("| shared | 100.00% | 90.00% | pass |", summary.read_text(encoding="utf-8"))

    def test_suggested_floors_ratchet_up_only(self):
        rows = [("shared", 97.26, 90.0, True), ("runtime", 80.5, 85.0, False), ("agent", None, 50.0, False)]
        self.assertEqual(coverage_gate.suggested(rows), {"shared": 96.2, "runtime": 85.0})

    def test_committed_floors_cover_the_safety_core(self):
        floors = coverage_gate.load_floors(ROOT / ".coverage-floors.json")
        for package in ("total", "services/executor", "services/coordinator", "services/guard", "shared"):
            self.assertIn(package, floors)
        self.assertGreaterEqual(floors["services/executor"], floors["total"])


if __name__ == "__main__":
    unittest.main()
