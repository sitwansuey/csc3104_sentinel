"""The release workflow only drafts: tag-triggered, least privilege, no publish or push."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


def jobs(text):
    """Job name -> its lines, from the two-space-indented keys under 'jobs:'."""
    body = text.split("\njobs:\n", 1)[1]
    found, current = {}, None
    for line in body.splitlines():
        key = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if key:
            current = key.group(1)
            found[current] = []
        elif current:
            found[current].append(line)
    return {name: "\n".join(lines) for name, lines in found.items()}


def run_blocks(text):
    """The shell text of every 'run:' step, single-line or block."""
    blocks, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        found = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not found:
            continue
        indent, rest = len(found.group(1)), found.group(2)
        if rest not in {"|", ">-", ">", "|-"}:
            blocks.append(rest)
            continue
        body = []
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) <= indent:
                break
            body.append(following)
        blocks.append("\n".join(body))
    return blocks


class ReleaseWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text(encoding="utf-8")
        cls.jobs = jobs(cls.text)

    def test_triggers_are_version_tags_and_manual_rehearsal_only(self):
        trigger = self.text.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("tags:", trigger)
        self.assertIn("'v[0-9]+.[0-9]+.[0-9]+'", trigger)
        self.assertIn("'v[0-9]+.[0-9]+.[0-9]+-rc.[0-9]+'", trigger)
        self.assertIn("workflow_dispatch:", trigger)
        for forbidden in ("branches", "pull_request", "schedule", "release:"):
            self.assertNotIn(forbidden, trigger)

    def test_write_permission_is_only_on_the_draft_job(self):
        top = self.text.split("\njobs:\n", 1)[0]
        self.assertRegex(top, r"\npermissions:\n  contents: read\n")
        self.assertEqual(self.text.count("contents: write"), 1)
        self.assertIn("contents: write", self.jobs["draft"])
        self.assertNotIn("permissions", self.jobs["build"])

    def test_draft_job_needs_a_pushed_tag_and_the_build(self):
        draft = self.jobs["draft"]
        self.assertIn("needs: build", draft)
        self.assertIn("github.event_name == 'push'", draft)
        self.assertIn("startsWith(github.ref, 'refs/tags/v')", draft)
        self.assertIn("sha256sum --check --strict SHA256SUMS", draft)

    def test_release_is_created_as_a_draft_and_nothing_is_published_or_pushed(self):
        self.assertIn("gh release create", self.jobs["draft"])
        self.assertIn("--draft", self.jobs["draft"])
        self.assertIn("--verify-tag", self.jobs["draft"])
        for forbidden in ("docker push", "gh release edit", "--draft=false", "git push", "git tag ", "--push",
                          "ghcr.io"):
            self.assertNotIn(forbidden, self.text)

    def test_build_job_runs_the_release_scripts(self):
        build = self.jobs["build"]
        self.assertIn("scripts/release/preflight.py --tag \"$TAG\" --only version waivers images", build)
        self.assertIn("scripts/release/build.py --tag \"$TAG\"", build)
        self.assertIn("scripts/release/notes.py --tag \"$TAG\"", build)
        self.assertIn("persist-credentials: false", build)
        self.assertIn("fetch-depth: 0", build)

    def test_untrusted_values_reach_shell_only_through_the_environment(self):
        blocks = run_blocks(self.text)
        self.assertGreaterEqual(len(blocks), 5)
        for block in blocks:
            self.assertNotIn("${{", block)

    def test_release_doc_describes_the_human_approvals(self):
        text = (ROOT / "docs/release.md").read_text(encoding="utf-8")
        for phrase in ("preflight.py", "release.yml", "SHA256SUMS", "draft", "publish", "evidence/releases/",
                       ".trivyignore", "never move"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
