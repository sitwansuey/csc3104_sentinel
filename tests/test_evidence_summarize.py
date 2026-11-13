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

    def add_escalated_agent_run(self, expected_outcome):
        """A PASS agent run that ended, as asked, in a saved ESCALATE with no Guard or execution record."""
        run = Path(self.temp.name) / "source" / ("e-" + str(expected_outcome))
        fx.write(run / "approved-rollback/model-calls.json", [{"seconds": 5, "role": "Recovery Planner"}])
        summary = {"status": "PASS", "started_at": "2026-11-11T10:10:00Z", "agent_model_calls": 1,
                   "checks": [fx.check("agent planning failure ended in a saved ESCALATE and NEEDS_REVIEW")],
                   "planning_journal": {"attempts": 3, "error_code": "BUDGET_EXCEEDED", "delivered": 1}}
        if expected_outcome is not None:
            summary["expected_outcome"] = expected_outcome
        fx.write(run / "summary.json", summary)
        collect.collect(run, "agent", "agent-e1", evidence_root=self.root / "evidence", context=fx.context(),
                        inferred_revision=fx.REVISION, inferred_basis="fixture")

    def agent_row(self):
        return next(row for row in summarize.agent_runs(summarize.load_bundles(self.root)) if row[0] == "0123456")

    def test_expected_escalation_passes_but_is_never_counted_as_recovered(self):
        self.add_escalated_agent_run("escalated")
        row = self.agent_row()
        self.assertEqual(row[2:5], ["4", "1/4", "3"])
        self.assertEqual(row[-1], "expected escalation x1; incomplete x1; model server unreachable x1")
        # Model calls and seconds stay the medians of the recovered run alone.
        self.assertEqual(row[5:7], ["3", "38"])
        escalated = next(b for b in summarize.load_bundles(self.root) if b.bundle_id == "agent-e1")
        self.assertEqual(summarize.outcome(escalated), "PASS")
        self.assertTrue(summarize.expected_escalation(escalated))

    def test_agent_runs_that_expected_recovery_still_count_as_recovered(self):
        for expected_outcome in ("recovered", None):
            with self.subTest(expected_outcome=expected_outcome):
                self.tearDown()
                self.setUp()
                self.add_escalated_agent_run(expected_outcome)
                self.assertEqual(self.agent_row()[2:5], ["4", "2/4", "2"])

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

    def test_admission_deny_resolution_reads_the_fence(self):
        collect.collect(fx.wedge_run(self.root / "source", "deny", scenario="admission-deny"), "executor-wedge",
                        "wedge-deny", evidence_root=self.root / "evidence", context=fx.context(),
                        inferred_revision="b" * 40, inferred_basis="fixture")
        bundle = next(b for b in summarize.load_bundles(self.root) if b.bundle_id == "wedge-deny")
        self.assertEqual(summarize.wedge_resolutions([bundle]), "admission-deny at bbbbbbb PASS: CONFLICT/FENCED on 5/5 "
                                                                "reconcile passes, second proposal APPLIED")


class Phase2DerivationTests(unittest.TestCase):
    """Catalog scenario runs, expected escalations, refused approvals and the injection gate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runs = self.root / "source"
        recorded = lambda revision: {"source_revision": revision * 40, "dirty": False}
        cases = {
            "r2-fail": dict(status="FAILED", actual="ESCALATE", source="planning_failure", writes=0,
                            state="NEEDS_REVIEW", matched=False, error_code="ABSTAINED"),
            "r2-pass": {},
            "r1-a": dict(scenario="R1"),
            "r1-b": dict(scenario="R1"),
            "n1-pass": dict(scenario="N1", actual="ESCALATE", source="rule", codes=("TEMPLATE_ALREADY_BASELINE",),
                            writes=0, state="NEEDS_REVIEW", expected="escalated",
                            error_code="TEMPLATE_ALREADY_BASELINE"),
            "flagged": dict(scenario="R1", flagged_lines=1),
            "finding": dict(scenario="R1", finding={"decided_by": "rule", "rules": ["override"]}),
        }
        for name, options in cases.items():
            run = fx.scenario_run(runs, name, **options)
            summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
            summary["provenance"] = recorded("8" if name.endswith("fail") else "d")
            fx.write(run / "summary.json", summary)
            collect.collect(run, "agent", name, evidence_root=self.root / "evidence", context=fx.context())
        connected = runs / "reject"
        fx.write(connected / "summary.json", {
            "status": "FAILED", "started_at": "2026-11-11T22:09:33Z", "checks": [],
            "error": "Operator did not approve ACT-1; no Executor write attempted",
            "operator_approval": {"mode": "reject", "approvals": [], "refusals": [{"action_id": "ACT-1"}]}})
        fx.write(connected / "approved-rollback/proposal.json", {"action": "ROLLBACK"})
        collect.collect(connected, "connected-harness", "reject", evidence_root=self.root / "evidence",
                        context=fx.context(), inferred_revision=fx.REVISION, inferred_basis="fixture")
        self.bundles = {b.bundle_id: b for b in summarize.load_bundles(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def pick(self, *names):
        return [self.bundles[name] for name in names]

    def test_scenario_outcomes_name_the_decision_and_never_hide_a_failure(self):
        value = summarize.scenario_outcomes(self.pick("r2-fail", "r2-pass", "r1-a", "r1-b", "n1-pass"))
        self.assertEqual(value, "; ".join([
            "R2 at 8888888 FAILED: ESCALATE from a planning failure (ABSTAINED), 0 Executor writes, NEEDS_REVIEW, "
            "did not match the pre-registered expected outcome, labels pending",
            "R2 at ddddddd PASS: ROLLBACK decided by the model, 1 Executor write, RECOVERED, matched the "
            "pre-registered expected outcome, labels pending",
            "R1 at ddddddd PASS x2: ROLLBACK decided by the model, 1 Executor write, RECOVERED, matched the "
            "pre-registered expected outcome, labels pending",
            "N1 at ddddddd PASS: ESCALATE decided by the rule (TEMPLATE_ALREADY_BASELINE), 0 Executor writes, "
            "NEEDS_REVIEW, matched the pre-registered expected outcome, labels pending"]))
        self.assertEqual(summarize.scenario_outcomes([]), "no scenario runs")

    def test_scenario_rows_list_every_catalog_run(self):
        rows = {row[0]: row[1:] for row in summarize.scenario_rows(list(self.bundles.values()))}
        self.assertNotIn("reject", rows)
        self.assertEqual(rows["r2-fail"], ["R2", "8888888", "FAILED", "ESCALATE", "from a planning failure (ABSTAINED)",
                                           "0", "NEEDS_REVIEW", "no", "pending"])

    def test_recovered_ratio_never_counts_an_expected_escalation(self):
        self.assertEqual(summarize.recovered_ratio(self.pick("r2-fail", "r2-pass", "n1-pass")), "1/3")
        self.assertEqual(summarize.pass_ratio(self.pick("r2-fail", "r2-pass", "n1-pass")), "2/3")
        self.assertEqual(summarize.expected_escalations(self.pick("n1-pass", "r2-pass")), "1/2")

    def test_injection_free_needs_a_clean_logs_result_and_no_finding(self):
        self.assertEqual(summarize.injection_free(self.pick("r1-a", "flagged", "finding", "reject")), "1/4")

    def test_refused_approval_counts_only_without_an_execution_record(self):
        self.assertEqual(summarize.approval_refusals(self.pick("reject", "r2-pass")), "1/2")

    def test_escalation_failure_class_names_the_planning_code(self):
        self.assertEqual(summarize.failure_class(summarize.summary_of(self.bundles["r2-fail"])),
                         "agent escalated (ABSTAINED)")


class ExclusionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)
        self.path = self.root / summarize.EXCLUSIONS

    def tearDown(self):
        self.temp.cleanup()

    def exclude(self, *entries):
        self.path.write_text(json.dumps({"schema": "sentinel-evidence-exclusions-v1", "excluded": list(entries)}),
                             encoding="utf-8")

    def test_no_register_means_no_exclusions(self):
        self.assertEqual(summarize.load_exclusions(self.root), [])
        self.assertEqual(summarize.exclusions_table(self.root), "_No excluded runs._")

    def test_excluded_runs_are_listed_with_their_reason(self):
        self.exclude({"run": "boutique-live-x", "class": "agent", "source_revision": "a" * 40,
                      "attempted_at": "2026-11-12T05:04:57Z", "reason": "Docker engine down"})
        self.assertEqual(summarize.verify_bundles(self.root), [])
        self.assertIn("| boutique-live-x | agent | aaaaaaa | 2026-11-12T05:04:57Z | Docker engine down |",
                      summarize.exclusions_table(self.root))
        self.assertEqual(run(self.root)[0], 0)
        self.assertIn("boutique-live-x", (self.root / "evidence/README.md").read_text(encoding="utf-8"))

    def test_an_exclusion_needs_a_reason_and_cannot_also_be_a_bundle(self):
        self.exclude({"run": "AGENT-A1", "class": "agent", "source_revision": "a" * 40, "reason": "x"},
                     {"run": "other", "class": "agent", "source_revision": "a" * 40, "reason": " "},
                     {"run": "other", "class": "agent", "source_revision": "a" * 40, "reason": "y"})
        problems = summarize.verify_bundles(self.root)
        self.assertIn("evidence/exclusions.json: AGENT-A1 is both excluded and collected as a bundle", problems)
        self.assertIn("evidence/exclusions.json: other needs a reason", problems)
        self.assertIn("evidence/exclusions.json: other is listed twice", problems)


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


class PairBundleTests(unittest.TestCase):
    """Scenario-pair bundles (P4.05) are listed with their pair result and ShopOutcome; labels stay pending."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        runs, evidence = self.root / "source", self.root / "evidence"
        for run, bundle_id in ((fx.pair_run(runs, "boutique-pair-20261113T100000Z-aaaaaa"), "pair-deadlock"),
                               (fx.pair_run(runs, "boutique-pair-20261113T090000Z-bbbbbb", status="FAILED",
                                            judged=False, started="2026-11-13T09:00:00Z"), "pair-stopped"),
                               (fx.pair_run(runs, "boutique-pair-20261113T110000Z-cccccc", result="RECOVERED",
                                            shop="RECOVERED", started="2026-11-13T11:00:00Z", labels="signed"),
                                "pair-recovered")):
            collect.collect(run, "scenario-pair", bundle_id, evidence_root=evidence, context=fx.context())
        for relative, names in summarize.TARGETS.items():
            fx.write(self.root / relative, skeleton(names))

    def test_pair_rows_list_every_run_with_its_result_shop_outcome_and_labels(self):
        rows = summarize.pair_rows(summarize.load_bundles(self.root))
        self.assertEqual(rows, [
            ["pair-stopped", "redis-pod+cart-config", "0123456", "FAILED", "no result", "unknown", "unmeasured",
             "none", "n/a", "pending"],
            ["pair-deadlock", "redis-pod+cart-config", "0123456", "PASS", "DEPENDENCY_DEADLOCK", "FAILED",
             "cartservice 0, redis-cart 0", "cartservice DEPENDENCY_DEADLOCK, redis-cart DEPENDENCY_DEADLOCK", "no",
             "pending"],
            ["pair-recovered", "redis-pod+cart-config", "0123456", "PASS", "RECOVERED", "RECOVERED",
             "cartservice 1, redis-cart 1", "none", "yes", "signed"]])

    def test_the_generated_block_and_the_runs_table_include_pair_bundles(self):
        code, output = run(self.root)
        self.assertEqual(code, 0, output)
        results = (self.root / "docs/results.md").read_text(encoding="utf-8")
        self.assertIn("| pair-deadlock | redis-pod+cart-config | 0123456 | PASS | DEPENDENCY_DEADLOCK | FAILED |",
                      block(results, "pair-runs"))
        self.assertIn("| scenario-pair | 3 | 2 | 1 | 0 | 0 | 3 recorded, 0 inferred |", block(results, "runs"))
        self.assertEqual(run(self.root, check=True)[0], 0)


class Phase3DerivationTests(unittest.TestCase):
    """Distributed scenarios, the seal, traces, the admission check, worker containers and kind lab sessions."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        runs, evidence = self.root / "source", self.root / "evidence"

        def add(run, run_class, bundle_id, attachments=(), date=None):
            collect.collect(run, run_class, bundle_id, evidence_root=evidence, context=fx.context(), date=date,
                            inferred_revision=fx.REVISION, inferred_basis="fixture", attachments=attachments)

        sealed = {"process": "executor-a", "ts": "2026-11-12T18:07:52.033Z", "event": "executor.seal.sealed",
                  "identity": "executor-a", "fence_epoch": 2, "basis": "FENCED"}
        conflict = lambda operation, reason, identity: {
            "process": "coordinator", "ts": "2026-11-12T18:07:52.300Z", "event": "http.access",
            "operation": operation, "http_status": 409, "reason": reason, "identity": identity}
        s8_notes = {"paused": "executor-b", "paused_result": "CONFLICT", "writes_after_release": 0}
        add(fx.distributed_run(runs, "s1b", visible=1.4), "distributed-scenario", "kind-s1b")
        add(fx.distributed_run(runs, "s2", scenario="S2", matches=False, failed=("I5",), states=("FAILED",),
                               started="2026-11-12T18:01:08Z", visible=21.7), "distributed-scenario", "kind-s2")
        add(fx.distributed_run(runs, "s8", scenario="S8", states=("FAILED",), writes=0, notes=s8_notes,
                               started="2026-11-12T18:07:27Z",
                               timeline=[sealed, conflict("renew", "lease_not_current", "executor-b"),
                                         conflict("result", "result_changed", "guard-1"),
                                         conflict("result", "result_changed", "executor-b")]),
            "distributed-scenario", "kind-s8")
        wrapper = fx.write(runs / "S1c-wrapper.json", {"inject": {"at": "2026-11-12T16:00:00Z"},
                                                       "fault_visible": {"at": "2026-11-12T16:00:20.100Z"}})
        add(fx.distributed_run(runs, "s1c", scenario="S1c", started="2026-11-12T16:01:16Z"), "distributed-scenario",
            "kind-s1c", attachments=((wrapper, "kind-wrapper.json"),))
        trace = [fx.span("observer.ingest", "a", process="observer-1", ts="2026-11-12T06:44:00.000Z", ms=0),
                 fx.span("coordinator.ingest", "b", "a", ts="2026-11-12T06:44:00.100Z", ms=40),
                 fx.span("worker.process", "c", "b", process="planner-1", ts="2026-11-12T06:44:04.500Z", ms=1000),
                 fx.span("worker.process", "d", "lost", process="planner-1", ts="2026-11-12T06:44:02.000Z", ms=10)]
        add(fx.distributed_run(runs, "s1a", scenario="S1a", mode="file", timeline=trace,
                               started="2026-11-12T06:44:05Z"), "distributed-scenario", "file-s1a")
        add(fx.admission_run(runs, "admission"), "executor-admission", "admission")
        add(fx.admission_run(runs, "admission-bad", failing=("lower epoch",)), "executor-admission", "admission-bad")
        add(fx.container_run(runs, "container"), "worker-container", "container")
        add(fx.container_run(runs, "container-503", unready_sample=True), "worker-container", "container-503")
        add(fx.container_run(runs, "container-old", orchestrated=False), "worker-container", "container-old",
            date="2026-11-12")
        rbac = [{"verb": "get", "expected": "yes", "answer": "yes"}, {"verb": "delete", "expected": "no",
                                                                      "answer": "no"}]
        lab = runs / "lab"
        fx.write(lab / "prepare.out", json.dumps({"rbac": rbac}))
        fx.write(lab / "restore-check.json", {"healthy": True, "seconds": 21.8, "after": {"template_is_baseline": True}})
        add(lab, "kind-lab-session", "lab", date="2026-11-12")
        old_lab = runs / "lab-old"
        fx.write(old_lab / "bundle/rbac-checks.json", rbac[:1] + [{"verb": "delete", "expected": "no",
                                                                  "answer": "yes"}])
        add(old_lab, "kind-lab-session", "lab-old", date="2026-11-12")
        self.bundles = {b.bundle_id: b for b in summarize.load_bundles(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def pick(self, *names):
        return [self.bundles[name] for name in names]

    def test_a_scenario_passes_only_when_it_matched_its_expectation(self):
        self.assertEqual(summarize.outcome(self.bundles["kind-s1b"]), "PASS")
        self.assertEqual(summarize.outcome(self.bundles["kind-s2"]), "FAILED")
        self.assertEqual(summarize.outcome(self.bundles["kind-s8"]), "PASS")
        bundle = self.bundles["kind-s1b"]
        bundle.manifest["files"] = [item for item in bundle.manifest["files"] if item["path"] != "summary.json"]
        self.assertEqual(summarize.outcome(bundle), "INCOMPLETE")

    def test_expectations_keep_the_failure_in_the_denominator_and_name_it(self):
        self.assertEqual(summarize.distributed_expectations(self.pick("kind-s1b", "kind-s2", "kind-s8")),
                         "2/3 matched their expectation (S1b 1/1, S2 0/1, S8 1/1); every invariant held in 2/3; "
                         "failed: S2 I5; 0 unobserved windows; 0 harness errors")

    def test_rows_read_the_fault_visibility_from_the_run_or_its_wrapper(self):
        rows = {row[0]: row for row in summarize.distributed_rows(list(self.bundles.values()))}
        self.assertEqual(rows["kind-s2"][1:], ["kind", "S2", "0123456", "FAILED", "FAILED", "1", "0", "I5", "50.8",
                                               "180", "21.7"])
        self.assertEqual(rows["kind-s1c"][-1], "20.1")
        self.assertEqual(rows["file-s1a"][-1], "n/a")

    def test_seal_names_who_sealed_and_only_the_paused_executors_conflicts(self):
        self.assertEqual(summarize.seal_outcomes(self.pick("kind-s8")),
                         "0123456 PASS: executor-b paused, executor-a sealed at fence epoch 2 (FENCED), the paused "
                         "patch CONFLICT, 0 writes after release, 0 committed template writes, incident FAILED, "
                         "HTTP 409 to executor-b: renew lease_not_current, result result_changed")
        self.assertIn("no Executor sealed at fence epoch n/a (no seal record)",
                      summarize.seal_outcomes(self.pick("kind-s1b")))

    def test_trace_shape_counts_spans_and_orphans(self):
        self.assertEqual(summarize.trace_shapes(self.pick("file-s1a")),
                         "S1a PASS: 1 trace, 4 spans over 4.5 s from observer.ingest, 1 span whose parent never ended")
        self.assertEqual(summarize.trace_shapes([]), "no traced runs")

    def test_admission_counts_each_expected_class_and_never_hides_a_failed_probe(self):
        self.assertEqual(summarize.admission_probes(self.pick("admission", "admission-bad")),
                         "5/5 probes as expected on Kubernetes 1.37 (2 allowed, 2 denied by the policy, all 2 "
                         "with the rule named and the object unchanged, 1 denied by RBAC); namespace, policy and "
                         "private credentials removed; 4/5 probes as expected on Kubernetes 1.37 (2 allowed, 2 denied "
                         "by the policy, all 2 with the rule named and the object unchanged, 1 denied by RBAC); "
                         "namespace, policy and private credentials removed")
        self.assertEqual(summarize.outcome(self.bundles["admission-bad"]), "FAILED")

    def test_admission_counts_policy_denials_without_the_rule_named_separately(self):
        record = {"passed": 3, "probes": [{}, {}, {}], "server": "1.37", "allowed": 1, "policy": 2,
                  "policy_named_unchanged": 1, "rbac": 0, "cleaned": True}
        original = summarize.admission_record
        summarize.admission_record = lambda bundle: record
        try:
            value = summarize.admission_probes([object()])
        finally:
            summarize.admission_record = original
        self.assertIn("2 denied by the policy, 1 of them with the rule named and the object unchanged", value)

    def test_container_readiness_counts_samples_while_the_coordinator_was_up(self):
        self.assertEqual(summarize.container_readiness(self.pick("container")),
                         "3/3 health samples Docker healthy with /readyz 200 from the first Docker-healthy one until "
                         "the Coordinator stopped (4 taken while it was up, 3/4 answering /readyz 200; longest "
                         "planning pass 89.6 s, highest contact age 19.1 s); /readyz 503 62.0 s and Docker unhealthy "
                         "126.0 s after the Coordinator stopped")
        self.assertEqual(summarize.outcome(self.bundles["container"]), "PASS")
        self.assertEqual(summarize.outcome(self.bundles["container-503"]), "FAILED")

    def test_a_container_run_without_orchestrator_records_is_incomplete_but_keeps_docker_health(self):
        bundle = self.bundles["container-old"]
        self.assertEqual(summarize.outcome(bundle), "INCOMPLETE")
        self.assertIn("no orchestrator health record", summarize.container_readiness([bundle]))
        self.assertEqual(summarize.container_health([bundle]), "planner-container: unhealthy, 3/5 kept checks failed")
        row = {row[0]: row for row in summarize.container_rows(list(self.bundles.values()))}["container-old"]
        self.assertEqual(row[2:6], ["INCOMPLETE", "not recorded", "not recorded", "89.6"])

    def test_lab_session_needs_every_rbac_answer_and_a_healthy_restore(self):
        self.assertEqual(summarize.lab_sessions(self.pick("lab", "lab-old")),
                         "0123456: Executor RBAC checks 2/2 as expected, the restore check returned the target "
                         "healthy at its baseline template in 21.8 s; 0123456: Executor RBAC checks 1/2 as expected, "
                         "no restore check")
        self.assertEqual(summarize.outcome(self.bundles["lab"]), "PASS")
        self.assertEqual(summarize.outcome(self.bundles["lab-old"]), "FAILED")


if __name__ == "__main__":
    unittest.main()
