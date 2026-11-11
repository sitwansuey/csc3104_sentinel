"""Release preflight with fake git, gh and gate runners; nothing reaches GitHub or Docker."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from scripts.release import preflight
from scripts.release.policy import IMAGES, REQUIRED_EVIDENCE

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 11, 12)
HEAD = "a" * 40
EVIDENCE_REVISION = "b" * 40
TAG = "v1.0.0-rc.1"


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class FakeRunner:
    """Answers the git, gh and gate commands preflight sends."""

    def __init__(self):
        self.status = ""
        self.head = HEAD
        self.base = HEAD
        self.tags = []
        self.log = (f"{'c' * 40}\x1fMerge pull request #7 from team/fix-guard\x1fFix the guard\n\x1e\n"
                    f"{'d' * 40}\x1fAdd release notes (#9)\x1f\x1e\n")
        self.changes = {EVIDENCE_REVISION: ""}
        self.runs = [{"status": "completed", "conclusion": "success", "url": "https://ci/1",
                      "createdAt": "2026-11-12T01:00:00Z"}]
        self.gate_failures = set()
        self.calls = []

    def __call__(self, args, timeout=120):
        self.calls.append(list(args))
        tool, rest = args[0], list(args[1:])
        if tool == "git":
            return self.git(rest)
        if tool == "gh":
            return done(json.dumps(self.runs)) if self.runs is not None else done("", 1, "gh: not logged in")
        script = rest[0] if rest[0] != "-m" else rest[1]
        name = next(name for name, command in preflight.GATE if command == rest)
        return done("", 1, f"{script} failed") if name in self.gate_failures else done("ok")

    def git(self, args):
        if args[:1] == ["status"]:
            return done(self.status)
        if args == ["rev-parse", "HEAD"]:
            return done(self.head + "\n")
        if args[:2] == ["rev-parse", "--verify"]:
            return done(self.base + "\n")
        if args[:2] == ["tag", "--list"]:
            if args[2] == "v*":
                return done("\n".join(self.tags))
            return done(args[2] if args[2] in self.tags else "")
        if args[:1] == ["log"]:
            return done(self.log)
        if args[:2] == ["diff", "--name-only"]:
            revision = args[2]
            if revision not in self.changes:
                return done("", 128, f"fatal: bad revision '{revision}'")
            return done(self.changes[revision])
        raise AssertionError(f"unexpected git call {args}")


def bundle(name, provenance="recorded", revision=EVIDENCE_REVISION, status="current", captured="2026-11-12T00:00:00Z"):
    return {"bundle_id": f"{name}-{provenance}-{revision[:4]}", "class": name, "captured_at": captured,
            "source_revision": revision, "provenance": provenance, "status": status, "reason": None}


def write_root(directory, *, version="1.0.0-rc.1", project="1.0.0rc1", bundles=None, waivers="# none\n",
               extra_dockerfile=None):
    root = Path(directory)
    root.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(f'[project]\nname = "sentinel"\nversion = "{project}"\n', encoding="utf-8")
    (root / ".trivyignore").write_text(waivers, encoding="utf-8")
    (root / "evidence").mkdir()
    bundles = [bundle(name) for name in REQUIRED_EVIDENCE] if bundles is None else bundles
    (root / "evidence/index.json").write_text(json.dumps({"bundles": bundles}), encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs/limitations.json").write_text(json.dumps({"entries": [
        {"id": "L-ONE", "title": "Open thing", "status": "open"}]}), encoding="utf-8")
    (root / "deploy/docker").mkdir(parents=True)
    for image in IMAGES:
        (root / image.dockerfile).write_text("FROM scratch\n", encoding="utf-8")
    if extra_dockerfile:
        (root / "deploy/docker" / extra_dockerfile).write_text("FROM scratch\n", encoding="utf-8")
    return root


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.runner = FakeRunner()

    def run_preflight(self, *argv, tag=TAG, **root_options):
        root = Path(self.directory.name, "repo")
        if root.exists():
            shutil.rmtree(root)
        write_root(root, **root_options)
        output = io.StringIO()
        with redirect_stdout(output):
            code = preflight.main(["--tag", tag, *argv], runner=self.runner, root=root, today=TODAY)
        rows = {}
        for line in output.getvalue().splitlines():
            found = re.match(r"^(\S.*?)\s{2,}(PASS|FAIL|SKIP)\s{2}(.*)$", line)
            if found:
                rows[found.group(1).strip()] = (found.group(2), found.group(3))
        return code, rows, output.getvalue()

    def assert_no_go(self, check, code, rows, text=None):
        self.assertEqual(code, 1)
        self.assertEqual(rows[check][0], "FAIL", rows)
        if text:
            self.assertIn(text, rows[check][1])

    def test_everything_passing_is_go(self):
        code, rows, output = self.run_preflight()
        self.assertEqual(code, 0, output)
        self.assertIn("GO: v1.0.0-rc.1", output)
        self.assertNotIn("NO-GO", output)
        self.assertTrue(all(status == "PASS" for status, _ in rows.values()), rows)
        self.assertEqual(sum(1 for check in rows if check.startswith("gate: ")), len(preflight.GATE))

    def test_dirty_tree_is_no_go(self):
        self.runner.status = " M services/guard/core.py\n"
        self.assert_no_go("clean", *self.run_preflight()[:2], "services/guard/core.py")

    def test_untracked_files_are_asked_for(self):
        self.run_preflight()
        self.assertIn(["git", "status", "--porcelain", "--untracked-files=normal"], self.runner.calls)

    def test_head_not_at_base_is_no_go(self):
        self.runner.base = "e" * 40
        self.assert_no_go("branch", *self.run_preflight()[:2], "origin/main")

    def test_red_ci_is_no_go(self):
        self.runner.runs = [{"status": "completed", "conclusion": "failure", "url": "https://ci/2",
                             "createdAt": "2026-11-12T02:00:00Z"}] + self.runner.runs
        self.assert_no_go("ci", *self.run_preflight()[:2], "failure")

    def test_older_green_run_does_not_hide_a_newer_red_one(self):
        self.runner.runs = self.runner.runs + [{"status": "completed", "conclusion": "failure",
                                                "url": "https://ci/3", "createdAt": "2026-11-12T03:00:00Z"}]
        self.assert_no_go("ci", *self.run_preflight()[:2], "https://ci/3")

    def test_ci_still_running_missing_or_unreadable_is_no_go(self):
        for runs, text in (([{"status": "in_progress", "conclusion": "", "url": "u", "createdAt": "z"}], "in_progress"),
                           ([], "no checks.yml run"), (None, "cannot read CI runs")):
            with self.subTest(text=text):
                self.runner.runs = runs
                self.assert_no_go("ci", *self.run_preflight()[:2], text)

    def test_ci_query_is_read_only_and_names_head(self):
        self.run_preflight()
        gh = [call for call in self.runner.calls if call[0] == "gh"]
        self.assertEqual(len(gh), 1)
        self.assertEqual(gh[0][1:3], ["run", "list"])
        self.assertIn(HEAD, gh[0])

    def test_inferred_evidence_is_no_go(self):
        bundles = [bundle(name, "inferred" if name == "agent" else "recorded") for name in REQUIRED_EVIDENCE]
        self.assert_no_go("evidence", *self.run_preflight(bundles=bundles)[:2], "agent: no current recorded bundle")

    def test_withdrawn_recorded_evidence_does_not_count(self):
        bundles = [bundle(name, status="withdrawn" if name == "guard-eval" else "current")
                   for name in REQUIRED_EVIDENCE]
        self.assert_no_go("evidence", *self.run_preflight(bundles=bundles)[:2], "guard-eval")

    def test_code_change_after_the_evidence_is_no_go(self):
        self.runner.changes[EVIDENCE_REVISION] = "services/executor/core.py\n"
        self.assert_no_go("evidence", *self.run_preflight()[:2], "services/executor/core.py")

    def test_docs_only_change_after_the_evidence_is_go(self):
        # git diff is limited to the code paths, so a docs-only commit leaves it empty.
        self.runner.changes[EVIDENCE_REVISION] = ""
        code, rows, output = self.run_preflight()
        self.assertEqual(code, 0, output)
        diff = next(call for call in self.runner.calls if call[1:3] == ["diff", "--name-only"])
        paths = diff[diff.index("--") + 1:]
        self.assertIn("services", paths)
        self.assertNotIn("docs", paths)
        self.assertNotIn("evidence", paths)

    def test_a_fresh_older_bundle_passes_when_the_newest_is_stale(self):
        stale = "f" * 40
        self.runner.changes[stale] = "agent/llm.py\n"
        bundles = [bundle(name) for name in REQUIRED_EVIDENCE]
        bundles.append(bundle("agent", revision=stale, captured="2026-11-12T09:00:00Z"))
        code, rows, output = self.run_preflight(bundles=bundles)
        self.assertEqual(code, 0, output)

    def test_unknown_evidence_revision_is_no_go(self):
        bundles = [bundle(name, revision="9" * 40) for name in REQUIRED_EVIDENCE]
        self.assert_no_go("evidence", *self.run_preflight(bundles=bundles)[:2], "not in this clone")

    def test_version_tag_mismatch_is_no_go(self):
        self.assert_no_go("version", *self.run_preflight(version="1.0.0", project="1.0.0")[:2], "VERSION is 1.0.0")

    def test_pyproject_version_mismatch_is_no_go(self):
        self.assert_no_go("version", *self.run_preflight(project="0.0.0")[:2], "pyproject.toml")

    def test_malformed_version_is_no_go(self):
        self.assert_no_go("version", *self.run_preflight(version="1.0")[:2], "X.Y.Z")

    def test_version_must_sort_above_existing_tags(self):
        self.runner.tags = ["v1.0.0", "not-a-version"]
        self.assert_no_go("version", *self.run_preflight()[:2], "v1.0.0")

    def test_existing_tag_is_no_go(self):
        self.runner.tags = [TAG]
        code, rows, _ = self.run_preflight()
        self.assert_no_go("tag", code, rows, "never move a tag")
        self.assertEqual(rows["version"][0], "PASS")

    def test_earlier_version_tags_are_fine(self):
        self.runner.tags = ["v0.9.0", "v0.9.1-rc.3"]
        code, rows, output = self.run_preflight()
        self.assertEqual(code, 0, output)

    def test_final_release_sorts_above_its_candidates(self):
        self.runner.tags = ["v1.0.0-rc.1", "v1.0.0-rc.2"]
        code, rows, output = self.run_preflight(tag="v1.0.0", version="1.0.0", project="1.0.0")
        self.assertEqual(code, 0, output)
        self.assertIn("since v1.0.0-rc.2", rows["notes"][1])
        code, rows, output = self.run_preflight(tag="v1.0.0-rc.2", version="1.0.0-rc.2", project="1.0.0rc2")
        self.assertEqual(rows["version"][0], "PASS", output)
        self.assert_no_go("tag", code, rows, "already exists")

    def test_waiver_problems_are_no_go(self):
        cases = {
            "no issue": ("# accepted\nCVE-2026-1111 exp:2026-12-01\n", "no issue link"),
            "expired": ("# tracked in #12\nCVE-2026-1111 exp:2026-11-01\n", "expired"),
            "too far": ("# tracked in #12\nCVE-2026-1111 exp:2027-06-01\n", "more than 90 days"),
            "no expiry": ("# tracked in #12\nCVE-2026-1111\n", "expected 'ID exp:YYYY-MM-DD'"),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                self.assert_no_go("waivers", *self.run_preflight(waivers=text)[:2], message)

    def test_valid_waivers_pass(self):
        text = "# Upstream has no release yet; tracked in #12\nCVE-2026-1111 exp:2026-12-01\nGHSA-abcd-1234-efgh exp:2026-12-02\n"
        code, rows, output = self.run_preflight(waivers=text)
        self.assertEqual(code, 0, output)
        self.assertIn("2 waiver(s)", rows["waivers"][1])

    def test_unlisted_dockerfile_is_no_go(self):
        self.assert_no_go("images", *self.run_preflight(extra_dockerfile="new.Dockerfile")[:2],
                          "deploy/docker/new.Dockerfile")

    def test_a_failing_gate_command_is_no_go(self):
        self.runner.gate_failures = {"mypy"}
        code, rows, _ = self.run_preflight()
        self.assert_no_go("gate: mypy", code, rows, "failed")
        self.assertEqual(rows["gate: ruff"][0], "PASS")

    def test_gate_runs_every_documented_quality_command(self):
        """The preflight gate and docs/testing.md must not drift apart."""
        text = (ROOT / "docs/testing.md").read_text(encoding="utf-8")
        commands = {" ".join(command) for _, command in preflight.GATE}
        documented = [script for script in re.findall(r"python(?:\.exe)? (scripts/[^\s`]+\.py(?: --?[^\s`]+)?)",
                                                      text.split("## Meaningful regression boundaries")[0])
                      if "--compile" not in script]
        self.assertGreaterEqual(len(set(documented)), 9, documented)
        for script in documented:
            with self.subTest(script=script):
                self.assertTrue(any(command.startswith(script) for command in commands), script)
        for module in ("-m ruff check", "-m mypy", "-m coverage run -m unittest discover"):
            self.assertTrue(any(command.startswith(module) for command in commands), module)

    def test_skipping_a_check_is_incomplete_not_go(self):
        code, rows, output = self.run_preflight("--skip", "gate", "ci")
        self.assertEqual(code, 2)
        self.assertIn("INCOMPLETE", output)
        self.assertEqual(rows["ci"][0], "SKIP")
        self.assertFalse(any(call[0] == "gh" for call in self.runner.calls))
        self.assertFalse(any(call[0] not in {"git", "gh"} for call in self.runner.calls))

    def test_only_runs_the_named_checks_and_says_so(self):
        code, rows, output = self.run_preflight("--only", "version", "waivers", "images")
        self.assertEqual(code, 0, output)
        self.assertEqual(set(rows), {"version", "waivers", "images"})
        self.assertIn("(only: version, waivers, images)", output)

    def test_unknown_check_name_is_refused(self):
        with self.assertRaises(SystemExit):
            self.run_preflight("--skip", "evrything")

    def test_malformed_tag_is_refused_before_any_command(self):
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            preflight.main(["--tag", "1.0.0"], runner=self.runner, root=ROOT, today=TODAY)
        self.assertEqual(self.runner.calls, [])

    def test_notes_are_written_and_counted(self):
        path = Path(self.directory.name, "notes.md")
        code, rows, output = self.run_preflight("--notes", str(path))
        self.assertEqual(code, 0, output)
        self.assertIn("2 merged PR(s) and 0 other commit(s)", rows["notes"][1])
        text = path.read_text(encoding="utf-8")
        self.assertIn("- #7 Fix the guard", text)
        self.assertIn("- #9 Add release notes", text)
        self.assertIn("L-ONE (open)", text)

    def test_preflight_never_writes_to_git_or_github(self):
        self.run_preflight("--notes", str(Path(self.directory.name, "n.md")))
        read_only = {"status", "rev-parse", "tag", "log", "diff"}
        for call in self.runner.calls:
            if call[0] == "git":
                self.assertIn(call[1], read_only, call)
                if call[1] == "tag":
                    self.assertEqual(call[2], "--list", call)
            if call[0] == "gh":
                self.assertEqual(call[1:3], ["run", "list"], call)

    def test_image_list_is_printed_as_json(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(preflight.main(["--images"], runner=self.runner), 0)
        self.assertEqual([item["name"] for item in json.loads(output.getvalue())], [image.name for image in IMAGES])
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
