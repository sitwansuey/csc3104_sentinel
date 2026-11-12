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


if __name__ == "__main__":
    unittest.main()
