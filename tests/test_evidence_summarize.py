"""Generated results tables: golden markdown, --check drift detection, honest denominators."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.evidence import collect, summarize
from tests.support import evidence_fixtures as fx

TIMELINE = [{"phase": "fault_injected", "at": "2026-11-11T10:00:50Z", "scenario": "primary"},
            {"phase": "incident_observed", "at": "2026-11-11T10:01:00Z", "scenario": "primary"},
            {"phase": "execution_recorded", "at": "2026-11-11T10:01:40Z", "scenario": "approved-rollback"}]


def skeleton(names):
    return "# Generated\n\nFree prose stays untouched.\n\n" + "\n".join(
        f"<!-- BEGIN GENERATED:{name} -->\n<!-- END GENERATED:{name} -->\n" for name in names)


def build(root):
    """Fixture evidence tree with passes, failures, an incomplete run and a withdrawn bundle."""
    root, runs = Path(root), Path(root) / "source"
    evidence = root / "evidence"

    def add(run, run_class, bundle_id, **options):
        collect.collect(run, run_class, bundle_id, evidence_root=evidence, context=fx.context(), **options)

    inferred = {"inferred_revision": fx.REVISION, "inferred_basis": "fixture"}
    add(fx.agent_run(runs, "a1"), "agent", "agent-a1", **inferred)
    add(fx.agent_run(runs, "a2", started="2026-11-11T12:00:00Z", seconds=(25, 10, 9), timeline=TIMELINE,
                     provenance={"source_revision": "f" * 40, "dirty": False}), "agent", "agent-a2")
    add(fx.agent_run(runs, "a3", status="FAILED", started="2026-11-11T10:05:00Z",
                     error="RuntimeError: Ollama Planner failed: connection actively refused"), "agent", "agent-a3",
        **inferred)
    incomplete = runs / "a4"
    fx.write(incomplete / "rbac-checks.json", [])
    add(incomplete, "agent", "agent-a4", date="2026-11-11", **inferred)
    add(fx.connected_run(runs, "c1"), "connected-harness", "connected-c1", **inferred)
    add(fx.connected_run(runs, "c2", passed=False), "connected-harness", "connected-c2", **inferred)
    add(fx.connected_run(runs, "c3"), "connected-harness", "connected-c3", **inferred)
    add(fx.rejection_run(runs, "r1"), "guard-rejection", "rejection-r1", **inferred)
    add(fx.chaos_run(runs, "ch1"), "chaos-batch", "chaos-ch1", **inferred)
    add(fx.chaos_run(runs, "ch2", faults=("dns",), runs=[], status="failed"), "chaos-batch", "chaos-ch2", **inferred)
    add(fx.security_run(runs, "s1"), "security-lab", "security-s1", **inferred)
    add(fx.observer_run(runs, "o1"), "observer-acceptance", "observer-o1", **inferred)
    index_path = evidence / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    for entry in index["bundles"]:
        if entry["bundle_id"] == "connected-c3":
            entry.update(status="withdrawn", reason="fixture: captured on a mislabelled cluster")
    index_path.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    claims = [
        {"id": "C-AGENT", "text": "Agent mode recovered in {value} runs", "derive": "pass_ratio",
         "bundles": ["agent-a1", "agent-a2", "agent-a3", "agent-a4"], "value": "2/4",
         "scope_caveat": "One fault type."},
        {"id": "C-REJECT", "text": "Guard rejection: {value}", "derive": "zero_write_rejections",
         "bundles": ["rejection-r1"], "value": "1/1 refused, 0 Executor writes", "scope_caveat": "One scenario."},
        {"id": "C-CHAOS", "text": "Chaos faults {value}", "derive": "chaos_injection",
         "bundles": ["chaos-ch1", "chaos-ch2"], "value": "injected 2/3, cleaned up 2/3",
         "scope_caveat": "Injection only."},
    ]
    (evidence / "claims.json").write_text(json.dumps({"schema": "sentinel-claims-v1", "claims": claims}, indent=2),
                                          encoding="utf-8")
    for relative, names in summarize.TARGETS.items():
        fx.write(root / relative, skeleton(names))
    return root


def run(root, check=False):
    out = io.StringIO()
    code = summarize.run(root, check=check, out=out)
    return code, out.getvalue()


def block(text, name):
    return summarize.BLOCK.search(text[text.index("<!-- BEGIN GENERATED:" + name):]).group("body")


GOLDEN = {
    "runs": """\
| Class | Runs | PASS | FAILED | INTERRUPTED | INCOMPLETE | Provenance |
| --- | --- | --- | --- | --- | --- | --- |
| agent | 4 | 2 | 1 | 0 | 1 | 1 recorded, 3 inferred |
| chaos-batch | 2 | 1 | 1 | 0 | 0 | 0 recorded, 2 inferred |
| connected-harness | 2 | 1 | 1 | 0 | 0 | 0 recorded, 2 inferred |
| guard-rejection | 1 | 1 | 0 | 0 | 0 | 0 recorded, 1 inferred |
| observer-acceptance | 1 | 1 | 0 | 0 | 0 | 0 recorded, 1 inferred |
| security-lab | 1 | 1 | 0 | 0 | 0 | 0 recorded, 1 inferred |
""",
    "agent-runs": """\
| Revision | Provenance | Runs | RECOVERED | Not recovered | Model calls (median) | Model seconds (median) | Failure classes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0123456 | inferred | 3 | 1/3 | 2 | 3 | 38 | incomplete x1; model server unreachable x1 |
| fffffff | recorded | 1 | 1/1 | 0 | 3 | 44 | none |
""",
    "recovery-checks": """\
| Class | Check | Passed / runs |
| --- | --- | --- |
| connected-harness | healthy baseline captured | 2/2 |
| connected-harness | approved real rollback | 2/2 |
| connected-harness | crash reconciles without resubmission | 1/2 |
""",
    "timings": """\
| Class | Interval | n | Median s | Min s | Max s | Source |
| --- | --- | --- | --- | --- | --- | --- |
| agent | fault -> incident observed | 1 | 10 | 10 | 10 | recorded |
| agent | incident observed -> repair applied | 2 | 42.5 | 40 | 45 | approx |
| agent | repair applied -> outcome window end | 2 | 57.5 | 55 | 60 | approx |
""",
    "chaos": """\
| Fault | Attempts | Injected | Cleaned up | Healthy recovery window |
| --- | --- | --- | --- | --- |
| dns | 1 | 0/1 | 0/1 | 0/1 |
| network | 1 | 1/1 | 1/1 | 1/1 |
| pod | 1 | 1/1 | 1/1 | 0/1 |
""",
    "guard-rejection": """\
| Runs | Refused | Executor writes for the refused action | Unmeasured |
| --- | --- | --- | --- |
| 1 | 1/1 | 0 | 0 |
""",
    "security-lab": """\
| Bundle | Outcome | Valid mTLS quote | Blocked probes | Shopping after restore |
| --- | --- | --- | --- | --- |
| security-s1 | PASS | yes | 8/8 | yes |
""",
    "observer": """\
| Bundle | Outcome | Detection s | Duplicate suppression s |
| --- | --- | --- | --- |
| observer-o1 | PASS | 7.4 | 61.9 |
""",
    "index": """\
| Class | Bundles | Recorded | Inferred | Superseded | Withdrawn |
| --- | --- | --- | --- | --- | --- |
| agent | 4 | 1 | 3 | 0 | 0 |
| chaos-batch | 2 | 0 | 2 | 0 | 0 |
| connected-harness | 3 | 0 | 3 | 0 | 1 |
| guard-rejection | 1 | 0 | 1 | 0 | 0 |
| observer-acceptance | 1 | 0 | 1 | 0 | 0 |
| security-lab | 1 | 0 | 1 | 0 | 0 |
""",
}


class SummarizeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def results(self):
        return (self.root / "docs/results.md").read_text(encoding="utf-8")

    def test_generated_markdown_matches_the_golden_tables(self):
        code, output = run(self.root)
        self.assertEqual(code, 0, output)
        text = self.results() + (self.root / "evidence/README.md").read_text(encoding="utf-8")
        for name, expected in GOLDEN.items():
            with self.subTest(block=name):
                self.assertEqual(block(text, name), expected)
        self.assertIn("Free prose stays untouched.", self.results())
        self.assertEqual(run(self.root, check=True)[0], 0)

    def test_rendering_is_deterministic(self):
        run(self.root)
        first = self.results()
        run(self.root)
        self.assertEqual(self.results(), first)

    def test_hand_edited_cell_fails_check_with_a_diff(self):
        run(self.root)
        path = self.root / "docs/results.md"
        path.write_text(self.results().replace("| 0123456 | inferred | 3 | 1/3 |", "| 0123456 | inferred | 3 | 3/3 |"),
                        encoding="utf-8")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("-| 0123456 | inferred | 3 | 3/3 |", output)
        self.assertIn("+| 0123456 | inferred | 3 | 1/3 |", output)

    def test_stale_tables_fail_check_before_regeneration(self):
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("stale or hand-edited", output)

    def test_crlf_checkouts_pass_check_and_keep_their_line_endings(self):
        run(self.root)
        path = self.root / "docs/results.md"
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
        self.assertEqual(run(self.root, check=True)[0], 0)
        run(self.root)
        self.assertNotIn(b"\r\r", path.read_bytes())
        self.assertEqual(path.read_bytes().count(b"\n"), path.read_bytes().count(b"\r\n"))

    def test_withdrawn_bundle_is_excluded_and_a_claim_citing_it_fails(self):
        run(self.root)
        self.assertNotIn("connected-c3", self.results())
        claims_path = self.root / "evidence/claims.json"
        registry = json.loads(claims_path.read_text(encoding="utf-8"))
        registry["claims"].append({"id": "C-WITHDRAWN", "text": "Connected {value}", "derive": "pass_ratio",
                                   "bundles": ["connected-c3"], "value": "0/0", "scope_caveat": "x"})
        claims_path.write_text(json.dumps(registry), encoding="utf-8")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("C-WITHDRAWN: bundle connected-c3 is withdrawn", output)

    def test_failed_and_incomplete_runs_stay_in_the_denominator(self):
        bundles = summarize.load_bundles(self.root)
        agents = [b for b in bundles if b.run_class == "agent"]
        self.assertEqual(summarize.pass_ratio(agents), "2/4")
        self.assertEqual([summarize.outcome(b) for b in agents], ["PASS", "PASS", "FAILED", "INCOMPLETE"])

    def test_missing_timeline_falls_back_to_approx_file_timings(self):
        bundles = [b for b in summarize.load_bundles(self.root) if b.bundle_id == "agent-a1"]
        rows = summarize.timings(bundles)
        self.assertEqual([row[1] for row in rows], ["incident observed -> repair applied",
                                                    "repair applied -> outcome window end"])
        self.assertEqual({row[-1] for row in rows}, {"approx"})

    def test_stale_claim_value_fails_check_and_write_mode_updates_it(self):
        run(self.root)
        claims_path = self.root / "evidence/claims.json"
        registry = json.loads(claims_path.read_text(encoding="utf-8"))
        registry["claims"][0]["value"] = "4/4"
        claims_path.write_text(json.dumps(registry), encoding="utf-8")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("C-AGENT: value '4/4' differs from the evidence ('2/4')", output)
        self.assertEqual(run(self.root)[0], 0)
        self.assertEqual(json.loads(claims_path.read_text(encoding="utf-8"))["claims"][0]["value"], "2/4")

    def test_tampered_bundle_file_fails_check(self):
        run(self.root)
        target = next((self.root / "evidence").rglob("rbac-checks.json"))
        target.write_text("[1]\n", encoding="utf-8")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("does not match its MANIFEST hash", output)

    def test_missing_markers_are_an_error(self):
        (self.root / "docs/results.md").write_text("# No markers\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(summarize.main(["--root", str(self.root), "--check"]), 1)
        with self.assertRaises(summarize.EvidenceError):
            run(self.root)


class ExecutorWedgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runs, evidence = self.root / "source", self.root / "evidence"
        for name, scenario, fixed, revision in (("before", "rv-race", False, "d" * 40),
                                                ("rv", "rv-race", True, "a" * 40),
                                                ("recreate", "delete-recreate", True, "a" * 40)):
            collect.collect(fx.wedge_run(runs, name, scenario=scenario, fixed=fixed), "executor-wedge",
                            "wedge-" + name, evidence_root=evidence, context=fx.context(),
                            inferred_revision=revision, inferred_basis="fixture")
        self.bundles = {b.bundle_id: b for b in summarize.load_bundles(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def test_outcome_maps_the_scenario_status(self):
        self.assertEqual(summarize.outcome(self.bundles["wedge-rv"]), "PASS")
        self.assertEqual(summarize.outcome(self.bundles["wedge-before"]), "FAILED")

    def test_missing_scenario_summary_is_incomplete(self):
        bundle = self.bundles["wedge-rv"]
        bundle.manifest["files"] = [item for item in bundle.manifest["files"] if item["path"] != "rv-race.json"]
        self.assertEqual(summarize.outcome(bundle), "INCOMPLETE")

    def test_rows_show_the_reconcile_result_and_the_second_proposal(self):
        rows = {row[0]: row for row in summarize.wedge_rows(list(self.bundles.values()))}
        self.assertEqual(rows["wedge-before"][1:], ["rv-race", "ddddddd", "inferred", "FAILED", "NEEDS_RECONCILIATION",
                                                    "5/5 NEEDS_RECONCILIATION", "not admitted"])
        self.assertEqual(rows["wedge-recreate"][5:], ["SUPERSEDED/TARGET_REPLACED", "5/5 SUPERSEDED", "APPLIED"])

    def test_resolution_claim_follows_the_cited_order(self):
        cited = [self.bundles["wedge-before"], self.bundles["wedge-rv"]]
        self.assertEqual(summarize.wedge_resolutions(cited),
                         "rv-race at ddddddd FAILED: NEEDS_RECONCILIATION on 5/5 reconcile passes, second proposal "
                         "not admitted; rv-race at aaaaaaa PASS: SUPERSEDED/ADVANCED_WITHOUT_EVIDENCE on 5/5 "
                         "reconcile passes, second proposal APPLIED")

    def test_runs_table_counts_the_failed_before_run(self):
        self.assertIn("| executor-wedge | 3 | 2 | 1 | 0 | 0 | 0 recorded, 3 inferred |",
                      summarize.runs_table(list(self.bundles.values())))


class PreflightTests(unittest.TestCase):
    def test_only_a_preflight_stop_before_injection_counts(self):
        with tempfile.TemporaryDirectory() as temp:
            root, runs = Path(temp), Path(temp) / "source"
            inferred = {"inferred_revision": fx.REVISION, "inferred_basis": "fixture"}
            for name, injected in (("p1", False), ("p2", True)):
                collect.collect(fx.preflight_run(runs, name, injected=injected), "agent", "pre-" + name,
                                evidence_root=root / "evidence", context=fx.context(), **inferred)
            collect.collect(fx.agent_run(runs, "ok"), "agent", "pre-ok", evidence_root=root / "evidence",
                            context=fx.context(), **inferred)
            bundles = {b.bundle_id: b for b in summarize.load_bundles(root)}
            self.assertEqual(summarize.preflight_stops([bundles["pre-p1"]]), "1/1")
            self.assertEqual(summarize.preflight_stops(list(bundles.values())), "1/3")
            self.assertEqual(summarize.fail_closed([bundles["pre-p1"]]), "1/1")


class FailureClassTests(unittest.TestCase):
    def test_known_failures_get_short_labels(self):
        cases = {"<urlopen error [WinError 10061] ... actively refused>": "model server unreachable",
                 "HTTP 404 Not Found: model 'x' not found": "model not installed",
                 "Ollama Planner failed for x:latest: HTTP Error 404: Not Found": "model not installed",
                 "Ollama preflight failed for x:y at 127.0.0.1:11434: model is not installed": "model not installed",
                 "Agent did not propose a supported rollback: ESCALATE": "agent escalated",
                 "Role 6 could not verify shipping recovery: NOT_RECOVERED": "not recovered"}
        for error, label in cases.items():
            self.assertEqual(summarize.failure_class({"error": error}), label)
        self.assertEqual(summarize.failure_class({"error": "odd", "error_type": "KeyError"}), "KeyError")


if __name__ == "__main__":
    unittest.main()
