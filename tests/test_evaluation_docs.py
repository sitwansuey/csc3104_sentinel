"""docs/evaluation.md (roadmap P4.07): generated from committed evidence, drift-checked, never a zero for a missing run.

The page's blocks come from scripts/evidence/summarize.py. These tests pin
that the bytes are deterministic, that an edited evidence number or an edited
page number fails summarize --check, and that evidence which is not
committed renders as "not yet run" and never as a zero count.
"""

from pathlib import Path
import re
import tempfile
import unittest

from scripts.evidence import collect, summarize
from tests.support import evidence_fixtures as fx
from tests.test_evidence_summarize import block, run, skeleton

PAGE = "docs/evaluation.md"
ABLATION = summarize.ABLATION_DIR + "/v1-deterministic/results.json"
REPORT = summarize.RCAEVAL_DIR + "/sample-ob-v1/report.json"
# A zero count shown where no run was committed: '0/0', a bare zero cell, or 'x0'.
ZERO = re.compile(r"\b0/0\b|\|\s*0\s*\||\bx0\b")


def cell(k, n, low, high):
    return {"k": k, "n": n, "rate": k / n, "wilson95": [low, high]}


def ablation_results(correct=50):
    common = {"missed_repair": cell(0, 50, 0.0, 0.0713), "false_rejection": cell(0, 50, 0.0, 0.0713),
              "errors": cell(0, 52, 0.0, 0.0688)}
    return {"schema": "sentinel-recovery-ablation-results-v1", "identity": {"policies": ["P0", "P1", "P1R"]},
            "scope": "decision-level replay on recorded and derived inputs; no recovery was performed",
            "rows": 64, "error_rows": 0, "labels_pending_review": 64, "cells": [
                {"group": "captured", "policy": "P0", "level": "V0", "cases": 52, "rows": 52,
                 "correct": cell(correct, 52, 0.8702, 0.9894), "unsafe": cell(2, 52, 0.0106, 0.1298), **common},
                {"group": "adversarial", "policy": "P0", "level": "V2", "cases": 12, "rows": 12,
                 "correct": cell(12, 12, 0.7575, 1.0), "unsafe": cell(0, 12, 0.0, 0.2425), **common,
                 "injection_success": cell(0, 12, 0.0, 0.2425)}]}


def acceptance_run(scenario, *, passed=True):
    """One sentinel-dist per-run summary, with the fields kind_acceptance.results_table reads."""
    return {"schema": "sentinel-dist-acceptance-run-v1", "scenario": scenario, "started_at": "2026-11-13T15:00:00Z",
            "passed": passed, "invariants_passed": passed, "expectation": {"met": passed, "failures": []},
            "audit": {"max_template_writes_per_action": 1}, "takeover_seconds": None, "coordinator_recovery": None,
            "synthetic_planner": True, "limits": "Four kind nodes on one Docker host."}


def build(root, *, evaluation=True):
    """A fixture tree with live, distributed and security bundles; evaluation adds a replay run and its corpus."""
    root, runs = Path(root), Path(root) / "source"
    evidence = root / "evidence"

    def add(path, run_class, bundle_id):
        collect.collect(path, run_class, bundle_id, evidence_root=evidence, context=fx.context(),
                        inferred_revision=fx.REVISION, inferred_basis="fixture")

    add(fx.connected_run(runs, "c1"), "connected-harness", "connected-c1")
    add(fx.chaos_recovery_run(runs, "n2", refusals=[fx.forced("ROLLBACK", codes=("ROLLBACK_ALREADY_AT_BASELINE",))]),
        "chaos-recovery", "chaos-n2")
    add(fx.pair_run(runs, "pair"), "scenario-pair", "pair-dep")
    add(fx.distributed_run(runs, "s1b"), "distributed-scenario", "kind-s1b")
    add(fx.security_run(runs, "s1"), "security-lab", "security-s1")
    if evaluation:
        fx.write(root / ABLATION, ablation_results())
        fx.write(root / summarize.REPLAY_MANIFEST, {"schema": "sentinel-replay-corpus-v1", "cases": [{}] * 3,
                                                   "counts": {"captured": 1, "derived": 1, "adversarial": 1},
                                                   "labels": {"cases": 3, "signed": 0, "status": "pending"}})
    for relative, names in summarize.TARGETS.items():
        fx.write(root / relative, skeleton(names))
    return root


class EvaluationPageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = build(Path(self.temp.name) / "a")

    def page(self, root=None):
        return ((root or self.root) / PAGE).read_text(encoding="utf-8")

    def test_the_page_is_a_summarize_target_so_check_covers_it(self):
        self.assertEqual(summarize.TARGETS[PAGE], tuple(summarize.evaluation_blocks(
            self.root, summarize.load_bundles(self.root))))

    def test_bytes_are_deterministic(self):
        twin = build(Path(self.temp.name) / "b")
        self.assertEqual(run(self.root)[0], 0)
        self.assertEqual(run(twin)[0], 0)
        first = (self.root / PAGE).read_bytes()
        self.assertEqual(first, (twin / PAGE).read_bytes())
        self.assertEqual(run(self.root)[0], 0)
        self.assertEqual((self.root / PAGE).read_bytes(), first)
        self.assertEqual(run(self.root, check=True)[0], 0)

    def test_an_edited_number_in_the_evidence_fails_check(self):
        self.assertEqual(run(self.root)[0], 0)
        fx.write(self.root / ABLATION, ablation_results(correct=49))
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("FAIL: generated blocks in docs/evaluation.md are stale or hand-edited", output)
        self.assertIn("-| captured | P0 | V0 | 52 | 50/52 [0.87, 0.99]", output)
        self.assertIn("+| captured | P0 | V0 | 52 | 49/52 [0.87, 0.99]", output)

    def test_an_edited_number_on_the_page_fails_check(self):
        self.assertEqual(run(self.root)[0], 0)
        path = self.root / PAGE
        text = path.read_text(encoding="utf-8")
        self.assertIn("50/52 [0.87, 0.99]", text)
        path.write_text(text.replace("50/52 [0.87, 0.99]", "51/52 [0.87, 0.99]"), encoding="utf-8", newline="\n")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("docs/evaluation.md", output)

    def test_an_edited_number_in_a_bundle_fails_check(self):
        self.assertEqual(run(self.root)[0], 0)
        summary = next((self.root / "evidence").glob("*/security-s1/validation.json"))
        summary.write_text(summary.read_text(encoding="utf-8").replace('"passed": true', '"passed": false'),
                           encoding="utf-8")
        code, output = run(self.root, check=True)
        self.assertEqual(code, 1)
        self.assertIn("security-s1: validation.json does not match its MANIFEST hash", output)

    def test_missing_evidence_renders_not_yet_run_never_zero(self):
        root = build(Path(self.temp.name) / "bare", evaluation=False)
        self.assertEqual(run(root)[0], 0)
        page = self.page(root)
        rcaeval = block(page, "evaluation-rcaeval")
        self.assertTrue(rcaeval.startswith("_Not yet run._ No RCAEval report is committed"), rcaeval)
        replay = block(page, "evaluation-replay")
        self.assertIn("Corpus: not built", replay)
        for name, _, _ in summarize.ABLATION_RUNS:
            self.assertRegex(replay, rf"\| {name} \| [^\n]*\| not yet run \| - \| - \| - \|")
        latency = block(page, "evaluation-latency")
        self.assertTrue(latency.startswith("_Not yet run._ No latency ledger"), latency)
        self.assertIn("**sentinel-dist (four kind nodes, P4.01).** _Not yet run._",
                      block(page, "evaluation-distributed"))
        threats = block(page, "evaluation-threats")
        self.assertIn("sentinel-dist: not yet run.", threats)
        self.assertIn("RCAEval reports committed: not yet run.", threats)
        self.assertIn("Replay label sets signed: no corpus.", threats)
        self.assertIn("No replay corpus is committed.", threats)
        self.assertIn("Guard fixture evaluation: not yet run.", threats)
        for name in ("evaluation-rcaeval", "evaluation-replay", "evaluation-latency"):
            self.assertIsNone(ZERO.search(block(page, name)), name)
        self.assertIn("Pre-registered replay runs committed: 0/5.", threats)

    def test_empty_evidence_tree_renders_every_section_as_not_yet_run(self):
        root = Path(self.temp.name) / "empty"
        for relative, names in summarize.TARGETS.items():
            fx.write(root / relative, skeleton(names))
        self.assertEqual(run(root)[0], 0)
        page = self.page(root)
        for name in ("evaluation-setup", "evaluation-rcaeval", "evaluation-guard", "evaluation-live",
                     "evaluation-security", "evaluation-latency"):
            self.assertTrue(block(page, name).startswith("_Not yet run._"), name)
            self.assertIsNone(ZERO.search(block(page, name)), name)

    def test_a_committed_model_run_and_report_replace_not_yet_run(self):
        fx.write(self.root / summarize.ABLATION_DIR / "v1-model-14b-16k/results.json",
                 {**ablation_results(), "identity": {"policies": ["P2", "P3", "P4"]}})
        fx.write(self.root / REPORT, {
            "split": "eval", "sample": "sample-ob-v1", "level": "service", "deviations": [],
            "methods": {"baro": {"n": 40, "AC@1": {"value": 0.5, "count": 20, "ci95": [0.35, 0.65]},
                                 "AC@3": {"value": 0.7, "ci95": [0.55, 0.82]}, "AC@5": {"value": 0.8, "ci95": None},
                                 "Avg@5": {"value": 0.66, "ci95": [0.55, 0.76]},
                                 "abstention_rate": {"count": 0}, "error_rate": {"count": 1}}},
            "paired": []})
        self.assertEqual(run(self.root)[0], 0)
        page = self.page()
        self.assertIn("| v1-model-14b-16k | P2, P3, P4 | qwen3:14b, context 16384, seeds 42 43 44 | committed | 64 | "
                      "0 | 64 |", block(page, "evaluation-replay"))
        rcaeval = block(page, "evaluation-rcaeval")
        self.assertIn("| baro | 40 | 0.500 [0.350, 0.650] | 0.700 [0.550, 0.820] | 0.800 | 0.660 [0.550, 0.760] | 0 "
                      "| 1 |", rcaeval)
        self.assertIn("Deviations: none found in the result files.", rcaeval)
        self.assertIn("RCAEval reports committed: 1.", block(page, "evaluation-threats"))

    def test_live_matrix_keeps_failures_and_counts_forced_refusals(self):
        runs = self.root / "source"
        collect.collect(fx.chaos_recovery_run(runs, "n2-bad", status="FAILED", started="2026-11-13T09:30:00Z",
                                              refusals=[fx.forced("RESTART", verdict=None, status="CONFLICT")]),
                        "chaos-recovery", "chaos-n2-bad", evidence_root=self.root / "evidence", context=fx.context(),
                        inferred_revision=fx.REVISION, inferred_basis="fixture")
        self.assertEqual(run(self.root)[0], 0)
        live = block(self.page(), "evaluation-live")
        self.assertIn("| chaos-recovery | 2 | 1/2 | FAILED x1 | 0 | 1 | not recorded x2 | not recorded x2 |", live)
        self.assertIn("| N2 | chaos-recovery | not recorded | 2 | 1/2 | RESTART x2 | 1/2 | 2 | RECOVERED x2 | "
                      "pending x2 |", live)
        self.assertIn("Forced wrong actions in catalog runs: 1/2 refused by the Guard with no Executor write, 1 stopped "
                      "without a Guard refusal and with no write, 0 with an Executor write, 0 unmeasured.", live)
        self.assertIn("Chaos Mesh injection-only batches: not yet run.", live)

    def test_threats_count_the_caveats_from_the_evidence(self):
        self.assertEqual(run(self.root)[0], 0)
        threats = block(self.page(), "evaluation-threats")
        self.assertIn("Replay label sets signed: 0/3.", threats)
        self.assertIn("Pair runs by label status: pending x1.", threats)
        self.assertIn("Catalog scenario runs by label status: pending x1.", threats)
        self.assertIn("File and kind distributed runs recording single_host true: 0/1.", threats)
        self.assertIn("Live runs that record a person approving: 0.", threats)
        self.assertIn("Live runs by approval: not recorded x3.", threats)
        self.assertIn("Of 3 replay cases, 1 derived and 1 adversarial ones are synthetic", threats)


class SentinelDistTests(unittest.TestCase):
    """A sentinel-dist (distributed-acceptance) bundle fills its table; a failed run is never hidden."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = build(Path(self.temp.name) / "tree")

    def add(self, bundle_id, runs):
        folder = self.root / "source" / bundle_id
        fx.write(folder / "summary.json", {"schema": "sentinel-dist-acceptance-v1",
                                           "started_at": "2026-11-13T15:00:00Z",
                                           "provenance": {"source_revision": fx.REVISION}, "runs": len(runs)})
        for index, summary in enumerate(runs, 1):
            fx.write(folder / f"2026-11-13/{summary['scenario']}-r{index}-150000z/summary.json", summary)
        collect.collect(folder, "distributed-acceptance", bundle_id, evidence_root=self.root / "evidence",
                        context=fx.context())

    def test_runs_fill_the_table_and_unrun_scenarios_say_so(self):
        self.add("sentinel-dist-a", [acceptance_run("S0"), acceptance_run("S0", passed=False)])
        self.assertEqual(run(self.root)[0], 0)
        page = (self.root / PAGE).read_text(encoding="utf-8")
        distributed = block(page, "evaluation-distributed")
        self.assertIn("2 runs from 1 bundles; planner: synthetic x2. Limits the harness records: Four kind nodes on "
                      "one Docker host.", distributed)
        self.assertIn("| S0 | 2 | 1/2 | 1/2 | 1/2 | 1 | - | - / - |", distributed)
        self.assertIn("| S9 | not yet run |", distributed)
        threats = block(page, "evaluation-threats")
        self.assertIn("sentinel-dist: 2 runs, on four kind nodes that are containers on one Docker host.", threats)
        self.assertIn("sentinel-dist 2/2;", threats)

    def test_outcome_needs_every_run_to_pass(self):
        self.add("sentinel-dist-pass", [acceptance_run("S0")])
        self.add("sentinel-dist-fail", [acceptance_run("S0"), acceptance_run("S2", passed=False)])
        self.add("sentinel-dist-empty", [])
        outcomes = {b.bundle_id: summarize.outcome(b) for b in summarize.load_bundles(self.root)
                    if b.run_class == "distributed-acceptance"}
        self.assertEqual(outcomes, {"sentinel-dist-pass": "PASS", "sentinel-dist-fail": "FAILED",
                                    "sentinel-dist-empty": "INCOMPLETE"})

    def test_a_run_summary_missing_a_table_field_is_an_evidence_error(self):
        broken = acceptance_run("S0")
        del broken["audit"]
        self.add("sentinel-dist-broken", [broken])
        with self.assertRaises(summarize.EvidenceError):
            summarize.evaluation_distributed(summarize.load_bundles(self.root))


class CommittedPageTests(unittest.TestCase):
    """The committed docs/evaluation.md matches the committed evidence byte for byte in its generated blocks."""

    def test_committed_page_is_current(self):
        root = summarize.ROOT
        text = (root / PAGE).read_bytes().decode("utf-8")
        blocks = summarize.evaluation_blocks(root, summarize.load_bundles(root))
        self.assertEqual(summarize.apply_blocks(text, blocks, summarize.TARGETS[PAGE], PAGE), text)

    def test_committed_page_keeps_the_interpretation_human(self):
        text = (summarize.ROOT / PAGE).read_text(encoding="utf-8")
        interpretation = text[text.index("## Interpretation"):]
        self.assertNotIn("GENERATED", interpretation)
        self.assertLess(text.index("END GENERATED:evaluation-threats"), text.index("## Interpretation"))


if __name__ == "__main__":
    unittest.main()
