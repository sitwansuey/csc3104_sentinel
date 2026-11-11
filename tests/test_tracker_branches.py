"""Branch-survey tests use a fake gh runner; nothing reaches GitHub."""
from datetime import datetime, UTC
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.tracker import branches

REPO = branches.REPOSITORY
NOW = datetime(2026, 11, 12, tzinfo=UTC)


class FakeGh:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args):
        self.calls.append(list(args))
        path = args[args.index("--method") + 2]
        base, _, query = path.partition("?")
        if base not in self.responses:
            raise branches.BranchError(f"gh api --method GET failed: HTTP 404 for {base}")
        value = self.responses[base]
        if isinstance(value, list):
            page = int(re.search(r"(?:^|&)page=(\d+)", query).group(1))
            value = value[(page - 1) * branches.PAGE:page * branches.PAGE]
        return json.dumps(value)


def branch(name, sha, protected=False):
    return {"name": name, "commit": {"sha": sha}, "protected": protected}


def repository(extra_branches=(), pulls=()):
    names = [branch("main", "m" * 40), branch("old-merged", "a" * 40), branch("idle-work", "b" * 40),
             branch("fresh-work", "c" * 40), branch("pr-base", "d" * 40), branch("release", "e" * 40, True),
             *extra_branches]
    responses = {f"repos/{REPO}": {"default_branch": "main"}, f"repos/{REPO}/branches": names,
                 f"repos/{REPO}/pulls": list(pulls) or [
                     {"number": 86, "head": {"ref": "elsewhere", "repo": {"full_name": REPO}},
                      "base": {"ref": "pr-base", "repo": {"full_name": REPO}}}]}
    dates = {"a": "2026-10-20T00:00:00Z", "b": "2026-10-26T00:00:00Z", "c": "2026-11-10T00:00:00Z",
             "d": "2026-11-11T00:00:00Z"}
    ahead = {"a": 0, "b": 3, "c": 1, "d": 0}
    for item in names[1:]:
        sha = item["commit"]["sha"]
        key = sha[0]
        responses[f"repos/{REPO}/compare/main...{sha}"] = {
            "status": "behind" if ahead.get(key, 0) == 0 else "diverged",
            "ahead_by": ahead.get(key, 0), "behind_by": 5}
        responses[f"repos/{REPO}/branches/{item['name'].replace('/', '%2F')}"] = {
            "commit": {"sha": sha, "commit": {"committer": {"date": dates.get(key, "2026-10-01T00:00:00Z")}}}}
    return responses


def run(argv=(), responses=None):
    fake = FakeGh(repository() if responses is None else responses)
    out, err = io.StringIO(), io.StringIO()
    with patch("sys.stderr", err):
        code = branches.main(list(argv), runner=fake, now=NOW, out=out)
    return code, out.getvalue(), err.getvalue(), fake


class BranchSurveyTests(unittest.TestCase):
    def test_merged_branch_is_archived_before_delete(self):
        code, out, _, _ = run()
        self.assertEqual(code, 0)
        line = next(line for line in out.splitlines() if "git tag -a archive/old-merged" in line)
        steps = line.strip().split(" && ")
        self.assertEqual(len(steps), 3)
        self.assertTrue(steps[0].startswith("git tag -a archive/old-merged " + "a" * 40))
        self.assertEqual(steps[1], "git push origin refs/tags/archive/old-merged")
        self.assertEqual(steps[2], f"git push --force-with-lease=refs/heads/old-merged:{'a' * 40} "
                                   "origin --delete refs/heads/old-merged")

    def test_delete_is_leased_on_the_surveyed_sha(self):
        _, out, _, _ = run(["--json"])
        rows = {row["name"]: row for row in json.loads(out)["branches"]}
        delete = rows["old-merged"]["commands"][2]
        self.assertIn(f"--force-with-lease=refs/heads/old-merged:{'a' * 40}", delete)
        self.assertNotIn("git push origin --delete", out)
        self.assertEqual(rows["old-merged"]["command_line"], " && ".join(rows["old-merged"]["commands"]))
        # Inactive branches get the tag steps only, still chained.
        self.assertEqual(rows["idle-work"]["command_line"].count(" && "), 1)

    def test_every_delete_in_json_output_follows_its_tag(self):
        _, out, _, _ = run(["--json"])
        survey = json.loads(out)
        for row in survey["branches"]:
            steps = row.get("commands", [])
            deletes = [i for i, step in enumerate(steps) if "--delete" in step]
            if deletes:
                self.assertEqual(row["kind"], "merged")
                self.assertTrue(steps[0].startswith("git tag -a archive/"))
                self.assertTrue(steps[1].startswith("git push origin refs/tags/archive/"))
                self.assertEqual(deletes, [2])

    def test_default_protected_and_open_pull_request_branches_are_never_proposed(self):
        _, out, _, _ = run(["--json"])
        rows = {row["name"]: row for row in json.loads(out)["branches"]}
        self.assertEqual(rows["main"]["kind"], "kept")
        self.assertEqual(rows["release"]["kind"], "kept")
        # Merged, but still the base of an open pull request.
        self.assertEqual(rows["pr-base"]["kind"], "kept")
        self.assertIn("#86 base", rows["pr-base"]["reason"])
        for name in ("main", "release", "pr-base"):
            self.assertNotIn("commands", rows[name])

    def test_unmerged_branches_are_never_deleted(self):
        _, out, _, _ = run(["--json"])
        rows = {row["name"]: row for row in json.loads(out)["branches"]}
        self.assertEqual(rows["idle-work"]["kind"], "inactive")
        self.assertFalse(any("--delete" in step for step in rows["idle-work"]["commands"]))
        self.assertEqual(rows["fresh-work"]["kind"], "kept")
        self.assertEqual(rows["old-merged"]["kind"], "merged")

    def test_inactive_threshold_is_configurable(self):
        _, out, _, _ = run(["--json", "--inactive-days", "60"])
        rows = {row["name"]: row for row in json.loads(out)["branches"]}
        self.assertEqual(rows["idle-work"]["kind"], "kept")

    def test_apply_is_always_refused_without_any_call(self):
        code, out, err, fake = run(["--apply"])
        self.assertEqual(code, 2)
        self.assertIn("dry-run only", err)
        self.assertEqual(out, "")
        self.assertEqual(fake.calls, [])

    def test_only_read_requests_are_made(self):
        _, _, _, fake = run()
        self.assertTrue(fake.calls)
        self.assertTrue(all(call[:3] == ["api", "--method", "GET"] for call in fake.calls))

    def test_failed_comparison_fails_closed_without_a_plan(self):
        responses = repository()
        del responses[f"repos/{REPO}/compare/main...{'a' * 40}"]
        code, out, err, _ = run(responses=responses)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertEqual(out, "")

    def test_truncated_branch_list_fails_closed(self):
        responses = repository()
        responses[f"repos/{REPO}/branches"] = [branch(f"b{i}", "f" * 40) for i in range(branches.PAGE)]
        with patch.object(branches, "MAX_PAGES", 1):
            code, out, err, _ = run(responses=responses)
        self.assertEqual(code, 2)
        self.assertIn("refusing to guess", err)
        self.assertEqual(out, "")

    def test_branch_that_moves_during_the_survey_stops_it(self):
        responses = repository()
        responses[f"repos/{REPO}/branches/old-merged"]["commit"]["sha"] = "9" * 40
        code, _, err, _ = run(responses=responses)
        self.assertEqual(code, 2)
        self.assertIn("moved during the survey", err)

    def test_unusual_branch_names_are_quoted_for_the_shell(self):
        responses = repository(extra_branches=[branch("odd;name", "a" * 39 + "1")])
        responses[f"repos/{REPO}/compare/main...{'a' * 39 + '1'}"] = {"status": "behind", "ahead_by": 0,
                                                                        "behind_by": 1}
        responses[f"repos/{REPO}/branches/odd%3Bname"] = {
            "commit": {"sha": "a" * 39 + "1", "commit": {"committer": {"date": "2026-10-01T00:00:00Z"}}}}
        _, out, _, _ = run(responses=responses)
        self.assertIn(f"git push '--force-with-lease=refs/heads/odd;name:{'a' * 39 + '1'}' "
                      "origin --delete 'refs/heads/odd;name'", out)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class PrintedCommandsAgainstLocalRemoteTests(unittest.TestCase):
    """Run the printed commands against a throwaway local bare repository."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.env = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                    "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid"}
        self.remote, self.work = root / "remote.git", root / "work"
        self.git(root, "init", "-q", "--bare", str(self.remote))
        self.git(root, "init", "-q", str(self.work))
        self.git(self.work, "remote", "add", "origin", str(self.remote))
        self.surveyed = self.commit("surveyed")
        self.git(self.work, "push", "-q", "origin", "HEAD:refs/heads/done")

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=self.env, capture_output=True, text=True,
                              check=False, timeout=60)

    def commit(self, message):
        self.assertEqual(self.git(self.work, "commit", "-q", "--allow-empty", "-m", message).returncode, 0)
        return self.git(self.work, "rev-parse", "HEAD").stdout.strip()

    def run_line(self, row):
        """Run the steps as '&&' would: stop at the first failure."""
        for step in branches.commands(row):
            result = self.git(self.work, *shlex.split(step)[1:])
            if result.returncode:
                return result
        return result

    def remote_branch(self):
        return self.git(self.remote, "rev-parse", "--verify", "-q", "refs/heads/done").stdout.strip()

    def test_branch_unchanged_since_survey_is_tagged_then_deleted(self):
        result = self.run_line({"name": "done", "sha": self.surveyed, "kind": "merged"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.remote_branch(), "")
        tagged = self.git(self.remote, "rev-parse", "refs/tags/archive/done^{commit}").stdout.strip()
        self.assertEqual(tagged, self.surveyed)

    def test_branch_moved_after_survey_is_not_deleted(self):
        later = self.commit("pushed while the list was under review")
        self.git(self.work, "push", "-q", "origin", "HEAD:refs/heads/done")
        result = self.run_line({"name": "done", "sha": self.surveyed, "kind": "merged"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stale info", result.stderr)
        self.assertEqual(self.remote_branch(), later)


if __name__ == "__main__":
    unittest.main()
