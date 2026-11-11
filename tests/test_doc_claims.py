"""Docs cite claims instead of hand-typed numbers, and known-stale status phrases cannot return."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.evidence import summarize
from tests.test_evidence_summarize import build

ROOT = Path(__file__).resolve().parents[1]
QUOTE = "<!-- BEGIN GENERATED:claim-c-agent -->\n<!-- END GENERATED:claim-c-agent -->\n"


class RepositoryDocClaimTests(unittest.TestCase):
    def test_no_stale_status_phrase_without_a_claim_id(self):
        self.assertEqual(summarize.stale_statements(ROOT, summarize.load_claims(ROOT)), [])

    def test_previously_stale_sentences_are_gone(self):
        texts = {name: (ROOT / name).read_text(encoding="utf-8") for name in (
            "README.md", "docs/testing.md", "docs/security.md", "docs/packaging.md", "docs/planner.md")}
        self.assertNotIn("Real-model acceptance is still pending", texts["README.md"])
        self.assertNotIn("pending rebuild/scan", texts["README.md"])
        self.assertNotIn("It remains deferred", texts["docs/testing.md"])
        self.assertNotIn("No new protected live run or CVE scan", texts["docs/security.md"])
        self.assertNotIn("No newly built/scanned image", texts["docs/packaging.md"])
        self.assertNotIn("7 of 7 runs", texts["docs/planner.md"])

    def test_agent_results_are_quoted_from_claims(self):
        for name in ("README.md", "docs/planner.md"):
            with self.subTest(doc=name):
                self.assertIn("<!-- BEGIN GENERATED:claim-c-agent-warm -->",
                              (ROOT / name).read_text(encoding="utf-8"))

    def test_coordinator_guide_never_targets_the_protected_lab_cluster(self):
        guide = (ROOT / "deploy/sentinel/COORDINATOR.md").read_text(encoding="utf-8")
        commands = "\n".join(line for line in guide.splitlines() if line.startswith(("$", "kubectl", "kind", "docker")))
        self.assertNotIn("sentinel-lab", commands)
        self.assertIn("$context = 'kind-sentinel-boutique-connected'", guide)


class StalePhraseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "docs").mkdir()
        self.claims = [{"id": "C-AGENT-WARM"}]

    def tearDown(self):
        self.temp.cleanup()

    def problems(self, text, name="README.md"):
        (self.root / name).write_text(text, encoding="utf-8")
        return summarize.stale_statements(self.root, self.claims)

    def test_stale_phrase_without_claim_fails(self):
        problems = self.problems("Intro.\n\nReal-model acceptance is still\npending.\n")
        self.assertEqual(len(problems), 1)
        self.assertIn("README.md: 'still pending' needs a claim id", problems[0])

    def test_claim_id_in_the_same_paragraph_passes(self):
        self.assertEqual(self.problems("Acceptance is still pending beyond claim `C-AGENT-WARM`.\n"), [])

    def test_unknown_claim_id_or_one_in_another_paragraph_fails(self):
        self.assertEqual(len(self.problems("Still pending (C-AGENT-NEVER).\n")), 1)
        self.assertEqual(len(self.problems("Still pending.\n\nSee C-AGENT-WARM.\n")), 1)

    def test_docs_and_crlf_files_are_checked(self):
        problems = self.problems("No new protected live run\r\nwas performed.\r\n", name="docs/security.md")
        self.assertEqual(len(problems), 1)
        self.assertIn("docs/security.md: 'no new protected live run'", problems[0])

    def test_check_fails_on_the_base_branch_wording(self):
        """The README and docs this change corrects would fail the check."""
        for name, line in (("README.md", "Real-model acceptance is still pending. See testing."),
                           ("docs/testing.md", "It remains deferred; passing injected-model tests does not count."),
                           ("docs/security.md", "No new protected live run or CVE scan was performed in this review."),
                           ("README.md", "describes the existing runner and its pending rebuild/scan.")):
            with self.subTest(name=name, line=line):
                self.assertEqual(len(self.problems(line + "\n", name=name)), 1)
                (self.root / name).unlink()


class ClaimQuoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)
        self.doc = self.root / "docs/planner.md"
        self.doc.write_bytes(("# Planner\n\nResults:\n\n" + QUOTE).encode("utf-8"))

    def tearDown(self):
        self.temp.cleanup()

    def run_summarize(self, check=False):
        out = io.StringIO()
        return summarize.run(self.root, check=check, out=out), out.getvalue()

    def test_quote_block_renders_value_and_scope_caveat(self):
        self.assertEqual(self.run_summarize()[0], 0)
        text = self.doc.read_text(encoding="utf-8")
        self.assertIn("> **C-AGENT:** Agent mode recovered in 2/4 runs.\n> Scope: One fault type.\n", text)
        self.assertEqual(self.run_summarize(check=True)[0], 0)

    def test_quote_block_one_folder_below_docs_is_filled_and_checked(self):
        """The instructions source (docs/instructions/) quotes claims; a stale quote there fails --check."""
        nested = self.root / "docs/instructions/GROUP05_INSTRUCTIONS.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("# Instructions\n\n" + QUOTE, encoding="utf-8")
        self.assertEqual(self.run_summarize()[0], 0)
        self.assertIn("> **C-AGENT:** Agent mode recovered in 2/4 runs.", nested.read_text(encoding="utf-8"))
        nested.write_text(nested.read_text(encoding="utf-8").replace("2/4", "4/4"), encoding="utf-8")
        code, output = self.run_summarize(check=True)
        self.assertEqual(code, 1)
        self.assertIn("docs/instructions/GROUP05_INSTRUCTIONS.md are stale or hand-edited", output)

    def test_hand_edited_quote_fails_check(self):
        self.run_summarize()
        self.doc.write_text(self.doc.read_text(encoding="utf-8").replace("2/4", "4/4"), encoding="utf-8")
        code, output = self.run_summarize(check=True)
        self.assertEqual(code, 1)
        self.assertIn("docs/planner.md are stale or hand-edited", output)

    def test_quote_of_an_unknown_claim_is_an_error(self):
        self.doc.write_text(QUOTE.replace("claim-c-agent", "claim-c-missing"), encoding="utf-8")
        with self.assertRaises(summarize.EvidenceError):
            self.run_summarize()

    def test_crlf_doc_keeps_its_line_endings(self):
        self.doc.write_bytes(self.doc.read_bytes().replace(b"\n", b"\r\n"))
        self.run_summarize()
        data = self.doc.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertIn(b"2/4", data)


class FailClosedDerivationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = build(self.temp.name)
        self.bundles = {b.bundle_id: b for b in summarize.load_bundles(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def test_failed_run_with_no_downstream_record_counts(self):
        self.assertEqual(summarize.fail_closed([self.bundles["agent-a3"]]), "1/1")

    def test_passing_and_incomplete_runs_do_not_count(self):
        self.assertEqual(summarize.fail_closed([self.bundles["agent-a1"], self.bundles["agent-a4"]]), "0/2")

    def test_a_run_that_reached_the_executor_does_not_count(self):
        bundle = self.bundles["agent-a3"]
        bundle.manifest = dict(bundle.manifest, files=bundle.manifest["files"] + [
            {"path": "approved-rollback/execution.json", "sha256": "0" * 64}])
        self.assertEqual(summarize.fail_closed([bundle]), "0/1")

    def test_other_failure_classes_do_not_count(self):
        bundle = self.bundles["agent-a3"]
        summary = json.loads((bundle.directory / "summary.json").read_text(encoding="utf-8"))
        summary.update(error="Agent did not propose a supported rollback: ESCALATE")
        summary["agent_failure"]["error"] = summary["error"]
        (bundle.directory / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        self.assertEqual(summarize.fail_closed([bundle]), "0/1")

    def test_repository_claim_cites_only_fail_closed_runs(self):
        claim = next(c for c in summarize.load_claims(ROOT) if c["id"] == "C-AGENT-FAIL-CLOSED")
        self.assertEqual(claim["derive"], "fail_closed")
        self.assertEqual(claim["value"], f"{len(claim['bundles'])}/{len(claim['bundles'])}")


class LimitationsCommandTests(unittest.TestCase):
    def test_strict_command_passes_on_this_tree(self):
        result = subprocess.run([sys.executable, "scripts/evidence/limitations.py", "--strict"],
                                cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
