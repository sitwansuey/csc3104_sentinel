"""Building GROUP05_INSTRUCTIONS.pdf with the standard-library renderer. Offline; nothing is installed."""

import contextlib
import io
from pathlib import Path
import re
import tempfile
import unittest

from scripts.instructions import build_pdf, pdf
from tests.test_instructions_source import GOOD, Fixture

ROOT = Path(__file__).resolve().parents[1]
PAGE = re.compile(rb"/Type\s*/Page(?!s)\b")


def pages(data):
    return len(PAGE.findall(data))


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.out = Path(self.fixture.root, "out")

    def tearDown(self):
        self.fixture.close()

    def build(self, **options):
        return build_pdf.build(self.fixture.root, self.out, revision_text="abc123", **options)

    def test_output_has_the_exact_name_pages_and_cover_version(self):
        path, problems = self.build(version_text="v1.0.0-rc.1", repository_url="https://example.invalid/repo",
                                    members="Member One, Member Two")
        self.assertEqual(problems, [])
        self.assertEqual(path.name, "GROUP05_INSTRUCTIONS.pdf")
        data = path.read_bytes()
        self.assertTrue(data.startswith(b"%PDF-1.4"))
        self.assertTrue(data.rstrip().endswith(b"%%EOF"))
        self.assertGreaterEqual(pages(data), 2)  # the cover and at least one page of content
        self.assertIn(b"/SentinelVersion (v1.0.0-rc.1)", data)
        self.assertIn(b"(Version: v1.0.0-rc.1)", data)
        self.assertIn(b"(Source revision: abc123)", data)
        self.assertNotIn(build_pdf.PLACEHOLDER.encode(), data)

    def test_version_comes_from_the_version_file_and_missing_cover_values_are_marked(self):
        (self.fixture.root / "VERSION").write_text("v2.0.0\n", encoding="utf-8")
        path, _problems = self.build()
        data = path.read_bytes()
        self.assertIn(b"/SentinelVersion (v2.0.0)", data)
        self.assertIn(build_pdf.PLACEHOLDER.encode(), data)
        (self.fixture.root / "VERSION").unlink()
        self.assertIn(b"/SentinelVersion (unreleased)", self.build()[0].read_bytes())

    def test_step_tags_are_printed_above_their_blocks(self):
        data = self.build()[0].read_bytes()
        self.assertIn(b"(Step S1 | windows | up to 60 s | needs docker)", data)
        self.assertIn(b"(Step L1 | linux | up to 2 min | needs network, cluster)", data)
        self.assertIn(b"(Step S2 | windows | up to 30 s | only if needed)", data)
        self.assertEqual(data.count(b"(Step "), 3)  # the untagged text block gets no label

    def test_table_cells_are_not_broken_inside_short_words(self):
        data = self.build()[0].read_bytes()
        self.assertIn(b"(Tool) Tj", data)
        self.assertIn(b"(Purpose) Tj", data)

    def test_the_build_is_deterministic(self):
        first = self.build()[0].read_bytes()
        self.assertEqual(first, self.build()[0].read_bytes())

    def test_a_source_that_fails_the_check_is_not_built(self):
        self.fixture.write(GOOD.replace("-Port", "-Prt"))
        path, problems = self.build()
        self.assertIsNone(path)
        self.assertEqual(problems, ["S1: scripts/demo.ps1 has no parameter -Prt"])
        self.assertFalse((self.out / build_pdf.OUTPUT_NAME).exists())

    def test_characters_the_base_fonts_cannot_print_are_refused(self):
        self.fixture.write(GOOD + "\nArrow \u2192 here.\n")
        path, problems = self.build()
        self.assertIsNone(path)
        self.assertEqual(problems, ["characters the PDF fonts cannot print: U+2192"])

    def test_markdown_option_writes_the_resolved_source(self):
        self.build(markdown=True)
        resolved = (self.fixture.root / "build/instructions/GROUP05_INSTRUCTIONS.md").read_text(encoding="utf-8")
        self.assertIn("| kind | Cluster |", resolved)

    def test_cli_reports_and_exits(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = build_pdf.main(["--root", str(self.fixture.root), "--output-dir", str(self.out),
                                   "--version", "v1"])
        self.assertEqual(code, 0)
        self.assertIn("GROUP05_INSTRUCTIONS.pdf", out.getvalue())
        self.fixture.write(GOOD.replace("--setup", "--set-up"))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(build_pdf.main(["--root", str(self.fixture.root), "--output-dir", str(self.out)]), 1)


class RendererTests(unittest.TestCase):
    def test_long_commands_wrap_with_the_shell_continuation_character(self):
        line = "powershell.exe -NoProfile -File .\\scripts\\x.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup"
        pieces = pdf.wrap_code(line, 40, "powershell")
        self.assertGreater(len(pieces), 1)
        self.assertTrue(all(piece.endswith(" `") for piece in pieces[:-1]))
        self.assertTrue(all(len(piece) <= 40 for piece in pieces))
        self.assertEqual(" ".join(piece.rstrip("`").strip() for piece in pieces), line)
        bash = pdf.wrap_code("bash scripts/run-sentinel.sh --setup --cleanup --connected --dashboard", 30, "bash")
        self.assertTrue(all(piece.endswith(" \\") for piece in bash[:-1]))

    def test_wrapping_never_splits_inside_quotes(self):
        pieces = pdf.wrap_code("docker info --format '{{.OSType}} and {{.Architecture}}' --extra", 40, "bash")
        self.assertTrue(all(piece.count("'") % 2 == 0 for piece in pieces), pieces)

    def test_inline_markup_becomes_styled_runs(self):
        runs = pdf.inline("Run `kind` with **care**, see [setup](https://example.invalid/x) and *notes*.")
        self.assertEqual(runs, [("regular", "Run "), ("mono", "kind"), ("regular", " with "), ("bold", "care"),
                                ("regular", ", see "), ("regular", "setup"),
                                ("regular", " (https://example.invalid/x)"), ("regular", " and "),
                                ("italic", "notes"), ("regular", ".")])
        self.assertEqual(pdf.inline("`<run folder>` and `<id>`"),
                         [("mono", "<run folder>"), ("regular", " and "), ("mono", "<id>")])

    def test_parser_recognises_the_block_kinds_and_drops_comments(self):
        text = ("# Title\n\nPara one\ncontinues.\n<!-- hidden -->\n\n- item\n  more\n1. first\n\n> quoted\n"
                "> still quoted\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n\n```text\ncode\n```\n")
        kinds = [(block.kind, block.text) for block in pdf.parse(text)]
        self.assertEqual(kinds[:5], [("heading", "Title"), ("paragraph", "Para one continues."),
                                     ("item", "item more"), ("item", "first"),
                                     ("quote", "quoted still quoted")])
        self.assertEqual(pdf.parse(text)[5].rows, (("A", "B"), ("1", "2")))
        self.assertEqual((pdf.parse(text)[6].kind, pdf.parse(text)[6].text), ("code", "code"))
        self.assertNotIn("hidden", " ".join(block.text for block in pdf.parse(text)))

    def test_wrapped_text_fits_the_width(self):
        runs = [("regular", "word " * 200)]
        for line in pdf.wrap(runs, 10, 200):
            self.assertLessEqual(sum(pdf.width(text, style, 10) for style, text in line), 200)
        long_word = pdf.wrap([("mono", "x" * 500)], 10, 100)
        self.assertTrue(all(pdf.width(text, "mono", 10) <= 100 for line in long_word for _style, text in line))

    def test_pdf_strings_are_escaped(self):
        data, unencodable = pdf.render("Text with (parens) and a back\\slash.", [], "footer", {"Title": "t"})
        self.assertEqual(unencodable, [])
        self.assertIn(b"\\(parens\\)", data)
        self.assertIn(b"back\\\\slash", data)


class RepositoryBuildTests(unittest.TestCase):
    def test_the_repository_source_builds(self):
        with tempfile.TemporaryDirectory() as temp:
            path, problems = build_pdf.build(ROOT, temp, version_text="v0-test", revision_text="test")
            self.assertEqual(problems, [])
            data = path.read_bytes()
            self.assertGreaterEqual(pages(data), 5)
            self.assertIn(b"/SentinelVersion (v0-test)", data)


if __name__ == "__main__":
    unittest.main()
