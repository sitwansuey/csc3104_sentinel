"""Markdown link checker: dead relative links and anchors fail, external links are skipped."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from scripts import check_doc_links

ROOT = Path(__file__).resolve().parents[1]


def tree(files):
    temp = tempfile.TemporaryDirectory()
    for name, text in files.items():
        path = Path(temp.name) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return temp


class DocLinkTests(unittest.TestCase):
    def test_dead_links_and_anchors_fail_and_good_ones_pass(self):
        files = {
            "README.md": "\n".join([
                "# Title", "## Run it again", "## Run it again",
                "[ok](docs/guide.md) [ok anchor](docs/guide.md#set-up-the-lab) [self](#title)",
                "[dup](#run-it-again-1) [dir](docs/) [code](tools/run.py#L3)",
                "[dead](docs/missing.md) [dead anchor](docs/guide.md#no-such-heading) [dead self](#nope)",
                "[web](https://example.com/missing) [mail](mailto:team@example.com)",
                "`[not a link](docs/inline-code.md)`",
                "```", "[not a link](docs/fenced.md)", "```",
                "[ref]: docs/also-missing.md",
            ]),
            "docs/guide.md": "# Guide\n\n## Set up the lab\n\n[back](../README.md#title)\n",
            "tools/run.py": "print('x')\n",
        }
        with tree(files) as temp:
            problems = check_doc_links.check(temp)
        self.assertEqual(sorted(problems), sorted([
            "README.md: dead link docs/missing.md",
            "README.md: dead anchor docs/guide.md#no-such-heading",
            "README.md: dead anchor #nope",
            "README.md: dead link docs/also-missing.md",
        ]))

    def test_heading_slugs_follow_github_rules(self):
        self.assertEqual(check_doc_links.slug("Evidence and cleanup"), "evidence-and-cleanup")
        self.assertEqual(check_doc_links.slug("Live `adapter` and CLI!"), "live-adapter-and-cli")
        self.assertEqual(check_doc_links.slug("Role 6: Verifier (outcomes)"), "role-6-verifier-outcomes")

    def test_ignored_and_vendored_folders_are_not_scanned(self):
        files = {"README.md": "# Ok\n", "node_modules/pkg/README.md": "[x](gone.md)\n",
                 "runs/r1/notes.md": "[x](gone.md)\n", ".tool/cache/notes.md": "[x](gone.md)\n",
                 ".github/README.md": "[x](gone.md)\n"}
        with tree(files) as temp:
            self.assertEqual(check_doc_links.check(temp), [".github/README.md: dead link gone.md"])

    def test_main_exits_non_zero_on_a_dead_link(self):
        output = io.StringIO()
        with tree({"README.md": "[x](gone.md)\n"}) as temp, contextlib.redirect_stdout(output):
            self.assertEqual(check_doc_links.main(["--root", temp]), 1)
        self.assertIn("FAIL: README.md: dead link gone.md", output.getvalue())

    def test_repository_documentation_has_no_dead_links(self):
        self.assertEqual(check_doc_links.check(ROOT), [])


if __name__ == "__main__":
    unittest.main()
