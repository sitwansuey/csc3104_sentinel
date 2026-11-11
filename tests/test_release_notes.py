"""Release notes from a fake git history and fixture registers."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.release import notes
from scripts.release.policy import IMAGES, ReleaseError

LOG = "".join([
    f"{'1' * 40}\x1fMerge pull request #12 from team/feat-x\x1fAdd the release preflight\n\nLonger body.\n\x1e\n",
    f"{'2' * 40}\x1fFix guard eligibility (#10)\x1f\x1e\n",
    f"{'3' * 40}\x1fMerge branch 'feature' into integration\x1f\x1e\n",
    f"{'4' * 40}\x1fMerge pull request #11 from team/empty-body\x1f\x1e\n",
    f"{'5' * 40}\x1fstyle: direct commit\x1fno PR\n\x1e\n",
])


class FakeGit:
    def __init__(self, tags=(), log=LOG):
        self.tags, self.log, self.calls = list(tags), log, []

    def __call__(self, args):
        self.calls.append(list(args))
        if args[:2] == ["tag", "--list"]:
            return "\n".join(self.tags) + "\n"
        if args[0] == "log":
            return self.log
        if args == ["rev-parse", "HEAD"]:
            return "f" * 40 + "\n"
        raise AssertionError(args)


def write_registers(root, limitations, bundles):
    (root / "docs").mkdir()
    (root / "evidence").mkdir()
    (root / "docs/limitations.json").write_text(json.dumps({"entries": limitations}), encoding="utf-8")
    (root / "evidence/index.json").write_text(json.dumps({"bundles": bundles}), encoding="utf-8")


LIMITATIONS = [
    {"id": "L-OPEN", "title": "Clock skew is not bounded", "status": "open"},
    {"id": "L-FIXED", "title": "Old bug", "status": "fixed"},
    {"id": "L-MITIGATED", "title": "Wedge in doubt", "status": "mitigated"},
]
BUNDLES = [
    {"bundle_id": "a", "status": "current", "provenance": "inferred"},
    {"bundle_id": "b", "status": "current", "provenance": "recorded"},
    {"bundle_id": "c", "status": "withdrawn", "provenance": "recorded"},
]


class MergedPullTests(unittest.TestCase):
    def test_merge_and_squash_titles_are_read_and_other_commits_counted(self):
        pulls, others = notes.merged_pulls(FakeGit(), None)
        self.assertEqual([(pull.number, pull.title) for pull in pulls],
                         [(10, "Fix guard eligibility"), (11, "(no title; branch empty-body)"),
                          (12, "Add the release preflight")])
        self.assertEqual(others, 2)

    def test_range_starts_at_the_previous_tag_on_the_first_parent_history(self):
        git = FakeGit()
        notes.merged_pulls(git, "v0.9.0")
        self.assertEqual(git.calls[-1][:2], ["log", "--first-parent"])
        self.assertEqual(git.calls[-1][-1], "v0.9.0..HEAD")
        notes.merged_pulls(git, None)
        self.assertEqual(git.calls[-1][-1], "HEAD")

    def test_empty_history_gives_no_pulls(self):
        self.assertEqual(notes.merged_pulls(FakeGit(log=""), None), ([], 0))


class PreviousTagTests(unittest.TestCase):
    def test_highest_earlier_version_wins_and_odd_tags_are_ignored(self):
        git = FakeGit(["v0.9.0", "v0.10.0-rc.2", "v0.10.0", "archive/old", "vnext", "v1.0.0-rc.1", "v1.0.0"])
        self.assertEqual(notes.previous_tag(git, "v1.0.0-rc.1"), "v0.10.0")
        self.assertEqual(notes.previous_tag(git, "v1.0.0"), "v1.0.0-rc.1")
        self.assertEqual(notes.previous_tag(git, "v1.0.0-rc.2"), "v1.0.0-rc.1")

    def test_only_tags_merged_into_head_are_considered(self):
        git = FakeGit(["v0.1.0"])
        notes.previous_tag(git, "v1.0.0")
        self.assertEqual(git.calls[0], ["tag", "--list", "v*", "--merged", "HEAD"])

    def test_first_release_has_no_previous_tag(self):
        self.assertIsNone(notes.previous_tag(FakeGit(), "v1.0.0-rc.1"))

    def test_malformed_tag_is_refused(self):
        with self.assertRaises(ReleaseError):
            notes.previous_tag(FakeGit(), "1.0.0")


class RenderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        write_registers(self.root, LIMITATIONS, BUNDLES)

    def test_notes_are_deterministic_and_complete(self):
        first = notes.generate("v1.0.0-rc.1", FakeGit(["v0.9.0"]), self.root)
        second = notes.generate("v1.0.0-rc.1", FakeGit(["v0.9.0"]), self.root)
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("# Sentinel v1.0.0-rc.1\n"))
        self.assertIn("Review it before publishing", first)
        self.assertIn("- Source revision: `" + "f" * 40 + "`", first)
        self.assertIn("- Previous release: v0.9.0", first)
        self.assertIn("- Pre-release: yes (release candidate)", first)
        self.assertIn("- #12 Add the release preflight", first)
        self.assertIn("2 other commit(s) reached the first-parent history without a pull request.", first)
        for image in IMAGES:
            self.assertIn(f"`{image.name}:1.0.0-rc.1`", first)

    def test_open_and_mitigated_limitations_are_listed_and_fixed_ones_are_not(self):
        text = notes.generate("v1.0.0", FakeGit(), self.root)
        self.assertIn("- L-OPEN (open): Clock skew is not bounded", text)
        self.assertIn("- L-MITIGATED (mitigated): Wedge in doubt", text)
        self.assertNotIn("L-FIXED", text)
        self.assertIn("- Pre-release: no", text)
        self.assertIn("- Previous release: none (first tagged release)", text)

    def test_inferred_evidence_is_called_history_and_withdrawn_is_not_counted(self):
        text = notes.generate("v1.0.0", FakeGit(), self.root)
        self.assertIn("2 current evidence bundles: 1 inferred, 1 recorded.", text)
        self.assertIn("history, not proof of this tag", text)

    def test_all_recorded_evidence_has_no_inferred_caveat(self):
        write_registers_root = Path(self.root, "only-recorded")
        write_registers_root.mkdir()
        write_registers(write_registers_root, [], [{"status": "current", "provenance": "recorded"}])
        text = notes.generate("v1.0.0", FakeGit(log=""), write_registers_root)
        self.assertNotIn("history, not proof", text)
        self.assertIn("- None recorded.", text)
        self.assertIn("## Merged pull requests\n\n- None.", text)

    def test_unfixable_scan_findings_are_disclosed(self):
        text = notes.generate("v1.0.0", FakeGit(), self.root)
        self.assertIn("Findings with no fix", text)
        self.assertIn("are not built or scanned here", text)

    def test_missing_register_is_an_error(self):
        (self.root / "docs/limitations.json").unlink()
        with self.assertRaises(ReleaseError):
            notes.generate("v1.0.0", FakeGit(), self.root)

    def test_main_writes_the_file_with_lf_endings(self):
        output = self.root / "out" / "RELEASE_NOTES.md"
        with patch.object(notes, "git_runner", lambda root=None: FakeGit()):
            code = notes.main(["--tag", "v1.0.0", "--revision", "e" * 40, "--output", str(output)])
        self.assertEqual(code, 0)
        data = output.read_bytes()
        self.assertNotIn(b"\r\n", data)
        self.assertIn(b"`" + b"e" * 40 + b"`", data)


if __name__ == "__main__":
    unittest.main()
