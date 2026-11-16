"""LaTeX claim macros generated from evidence/claims.json. Offline."""

import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts.submission import claims_tex

ROOT = Path(__file__).resolve().parents[1]
MACRO_NAME = re.compile(r"^Claim[A-Za-z]+$")


def claim(claim_id, value="2/4", text="Agent mode recovered in {value} runs", caveat="One fault type.", bundles=2):
    return {"id": claim_id, "text": text, "value": value, "scope_caveat": caveat,
            "bundles": [f"b{index}" for index in range(bundles)], "derive": "pass_ratio"}


class MacroTests(unittest.TestCase):
    def test_names_are_letters_only(self):
        self.assertEqual(claims_tex.macro_base("C-AGENT-WARM"), "ClaimAgentWarm")
        self.assertEqual(claims_tex.macro_base("C-T1"), "ClaimTOne")
        self.assertEqual(claims_tex.macro_base("C-GUARD-EVAL2"), "ClaimGuardEvalTwo")
        with self.assertRaises(claims_tex.ClaimsError):
            claims_tex.macro_base("agent-warm")

    def test_every_repository_claim_gets_four_valid_unique_macros(self):
        claims, _digest = claims_tex.load(ROOT)
        names = [name for name, _body in claims_tex.macros(claims)]
        self.assertEqual(len(names), 4 * len(claims))
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(all(MACRO_NAME.match(name) for name in names), names)

    def test_values_statements_caveats_and_bundle_counts(self):
        bodies = dict(claims_tex.macros([claim("C-AGENT")]))
        self.assertEqual(bodies, {"ClaimAgent": "2/4", "ClaimAgentStatement": "Agent mode recovered in 2/4 runs",
                                  "ClaimAgentCaveat": "One fault type.", "ClaimAgentBundles": "2"})

    def test_latex_special_characters_are_escaped(self):
        bodies = dict(claims_tex.macros([claim("C-X", value="50% of #1 & $2_a {b} ~c ^d \\e")]))
        self.assertEqual(bodies["ClaimX"], r"50\% of \#1 \& \$2\_a \{b\} \textasciitilde{}c \textasciicircum{}d "
                                           r"\textbackslash{}e")

    def test_two_claims_with_the_same_macro_name_are_refused(self):
        with self.assertRaisesRegex(claims_tex.ClaimsError, r"C-T1 and C-T-ONE both map to \\ClaimTOne"):
            claims_tex.macros([claim("C-T1"), claim("C-T-ONE")])
        with self.assertRaisesRegex(claims_tex.ClaimsError, r"C-AGENT and C-AGENT both map"):
            claims_tex.macros([claim("C-AGENT"), claim("C-AGENT")])
        with self.assertRaisesRegex(claims_tex.ClaimsError, r"ClaimAgentCaveat"):
            claims_tex.macros([claim("C-AGENT"), claim("C-AGENT-CAVEAT")])

    def test_render_defines_each_macro_once_with_the_source_digest(self):
        text = claims_tex.render([claim("C-AGENT"), claim("C-OTHER", value="1/1")], "ab" * 32)
        self.assertIn("% evidence/claims.json sha256: " + "ab" * 32, text)
        self.assertIn("\\newcommand{\\ClaimAgent}{2/4}\n", text)
        self.assertIn("\\newcommand{\\ClaimOther}{1/1}\n", text)
        self.assertEqual(text.count("\\newcommand"), 8)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "evidence").mkdir()
        self.write([claim("C-AGENT"), claim("C-OBSERVER", value="3/3")])

    def tearDown(self):
        self.temp.cleanup()

    def write(self, claims):
        (self.root / claims_tex.CLAIMS).write_text(json.dumps({"schema": "sentinel-claims-v1", "claims": claims}),
                                                   encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = claims_tex.main(["--root", str(self.root), *args])
        return code, out.getvalue(), err.getvalue()

    def test_macros_used_in_a_source_must_be_defined(self):
        source = self.root / "main.tex"
        source.write_text("We recovered in \\ClaimAgent{} runs (\\ClaimAgentCaveat).\n", encoding="utf-8")
        self.assertEqual(self.run_cli("--check-source", str(source))[0], 0)
        source.write_text("We recovered in \\ClaimAgentWarm{} runs.\n", encoding="utf-8")
        code, output, _err = self.run_cli("--check-source", str(source))
        self.assertEqual(code, 1)
        self.assertIn("uses \\ClaimAgentWarm, which evidence/claims.json does not define", output)

    def test_claim_ids_named_in_a_source_must_exist(self):
        source = self.root / "outline.md"
        source.write_text("Rows cite `C-AGENT` and `C-AGENT-WARM`; L-ONE-FAULT is a limitation id.\n",
                          encoding="utf-8")
        code, output, _err = self.run_cli("--check-source", str(source))
        self.assertEqual(code, 1)
        self.assertEqual(output.strip(), f"FAIL: {source}: names claim C-AGENT-WARM, which is not in "
                                         "evidence/claims.json")

    def test_a_claim_removed_from_claims_json_fails_a_source_that_still_uses_it(self):
        source = self.root / "main.tex"
        source.write_text("\\ClaimObserver\n", encoding="utf-8")
        self.assertEqual(self.run_cli("--check-source", str(source))[0], 0)
        self.write([claim("C-AGENT")])
        self.assertEqual(self.run_cli("--check-source", str(source))[0], 1)

    def test_output_file_is_written(self):
        target = self.root / "article/generated/claims.tex"
        code, output, _err = self.run_cli("--output", str(target))
        self.assertEqual(code, 0)
        self.assertIn("Wrote 2 claims", output)
        self.assertIn("\\newcommand{\\ClaimObserver}{3/3}", target.read_text(encoding="utf-8"))

    def test_verify_refuses_when_the_evidence_check_fails(self):
        with patch.object(claims_tex, "verify", return_value=(False, "claims are stale")):
            code, output, err = self.run_cli("--verify")
        self.assertEqual((code, output), (1, ""))
        self.assertIn("claims are stale", err)
        with patch.object(claims_tex, "verify", return_value=(True, "")):
            self.assertEqual(self.run_cli("--verify")[0], 0)

    def test_missing_or_malformed_claims_file_is_an_error(self):
        (self.root / claims_tex.CLAIMS).write_text("{", encoding="utf-8")
        self.assertEqual(self.run_cli()[0], 2)
        self.write([{"id": "C-AGENT", "text": "x"}])
        code, _out, err = self.run_cli()
        self.assertEqual(code, 2)
        self.assertIn("lacks value, scope_caveat, bundles", err)
        (self.root / claims_tex.CLAIMS).unlink()
        self.assertEqual(self.run_cli()[0], 2)


class RepositoryTests(unittest.TestCase):
    def test_submission_docs_name_only_existing_claims_and_macros(self):
        claims, _digest = claims_tex.load(ROOT)
        paths = sorted((ROOT / "docs/submission").glob("*.md")) + [ROOT / "docs/instructions/GROUP05_INSTRUCTIONS.md"]
        self.assertEqual(claims_tex.source_problems(paths, claims), [])

    def test_verify_passes_on_the_committed_evidence(self):
        passed, output = claims_tex.verify(ROOT)
        self.assertTrue(passed, output)


if __name__ == "__main__":
    unittest.main()
