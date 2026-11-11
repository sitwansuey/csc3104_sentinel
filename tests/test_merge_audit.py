"""Merge-audit tests use a fake read-only API; nothing reaches GitHub."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("merge_audit", ROOT / ".github/scripts/merge_audit.py")
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)
REPO = audit.REPOSITORY


class FakeAPI:
    """Answers GET paths from a dict; a missing path behaves like an HTTP 404."""

    def __init__(self, responses):
        self.responses = responses
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        for prefix, value in self.responses.items():
            if path == prefix or path.startswith(prefix + "?"):
                if isinstance(value, Exception):
                    raise value
                return value
        raise audit.AuditError(f"GitHub returned HTTP 404 for {path.split('?')[0]}")


def review(login, state):
    return {"user": {"login": login}, "state": state}


def repo_with(reviews, checks=None, author="alice", number=7, sha="m1"):
    checks = [{"name": "test", "status": "completed", "conclusion": "success"}] if checks is None else checks
    return FakeAPI({
        f"repos/{REPO}/commits/{sha}/pulls": [{"number": number, "merged_at": "2026-11-11T00:00:00Z",
                                                "base": {"ref": "main"}}],
        f"repos/{REPO}/pulls/{number}": {"number": number, "title": "Example", "user": {"login": author},
                                         "head": {"sha": "head1"}},
        f"repos/{REPO}/pulls/{number}/reviews": reviews,
        f"repos/{REPO}/commits/head1/check-runs": {"total_count": len(checks), "check_runs": checks},
    })


class MergeAuditTests(unittest.TestCase):
    def test_author_only_approval_is_flagged(self):
        rows = audit.audit(repo_with([review("alice", "APPROVED")]), ["m1"])
        self.assertEqual(rows[0]["approvers"], [])
        self.assertIn("no approving review from someone other than the author", rows[0]["problems"])

    def test_author_match_ignores_case(self):
        rows = audit.audit(repo_with([review("Alice", "APPROVED")]), ["m1"])
        self.assertTrue(rows[0]["problems"])

    def test_non_author_approval_and_green_checks_pass(self):
        rows = audit.audit(repo_with([review("bob", "APPROVED")]), ["m1"])
        self.assertEqual(rows[0]["approvers"], ["bob"])
        self.assertEqual(rows[0]["problems"], [])

    def test_changes_requested_then_approved_by_same_reviewer_passes(self):
        rows = audit.audit(repo_with([review("bob", "CHANGES_REQUESTED"), review("bob", "COMMENTED"),
                                      review("bob", "APPROVED")]), ["m1"])
        self.assertEqual(rows[0]["problems"], [])

    def test_approval_later_withdrawn_is_flagged(self):
        for later in ("CHANGES_REQUESTED", "DISMISSED"):
            rows = audit.audit(repo_with([review("bob", "APPROVED"), review("bob", later)]), ["m1"])
            self.assertTrue(rows[0]["problems"], later)

    def test_comment_only_review_is_not_an_approval(self):
        rows = audit.audit(repo_with([review("bob", "COMMENTED")]), ["m1"])
        self.assertTrue(rows[0]["problems"])

    def test_failed_pending_or_missing_checks_are_flagged(self):
        cases = {"failed: test": [{"name": "test", "status": "completed", "conclusion": "failure"}],
                 "pending: test": [{"name": "test", "status": "in_progress", "conclusion": None}],
                 "none found": []}
        for expected, checks in cases.items():
            rows = audit.audit(repo_with([review("bob", "APPROVED")], checks=checks), ["m1"])
            self.assertEqual(rows[0]["checks"], expected)
            self.assertIn("checks " + expected, rows[0]["problems"])

    def test_direct_push_without_pull_request_is_flagged(self):
        api = FakeAPI({f"repos/{REPO}/commits/d1/pulls": []})
        rows = audit.audit(api, ["d1"])
        self.assertEqual(rows[0]["problems"], ["pushed without a merged pull request"])

    def test_pull_merged_into_another_branch_does_not_count(self):
        api = FakeAPI({f"repos/{REPO}/commits/d1/pulls": [
            {"number": 3, "merged_at": "2026-11-01T00:00:00Z", "base": {"ref": "dev/integration"}}]})
        self.assertEqual(audit.audit(api, ["d1"])[0]["problems"], ["pushed without a merged pull request"])

    def test_http_404_fails_closed_as_unverified(self):
        rows = audit.audit(FakeAPI({}), ["m1"])
        self.assertTrue(rows[0]["problems"][0].startswith("UNVERIFIED: GitHub returned HTTP 404"))
        api = repo_with([review("bob", "APPROVED")])
        del api.responses[f"repos/{REPO}/pulls/7/reviews"]
        rows = audit.audit(api, ["m1"])
        self.assertEqual(rows[0]["approvers"], [])
        self.assertTrue(rows[0]["problems"][0].startswith("UNVERIFIED"))

    def test_truncated_reviews_or_checks_fail_closed(self):
        api = repo_with([review("bob", "APPROVED")] * audit.PAGE)
        with patch.object(audit, "MAX_PAGES", 1):
            rows = audit.audit(api, ["m1"])
        self.assertIn("truncated", rows[0]["problems"][0])
        api = repo_with([review("bob", "APPROVED")])
        api.responses[f"repos/{REPO}/commits/head1/check-runs"] = {"total_count": 150, "check_runs": []}
        self.assertIn("truncated", audit.audit(api, ["m1"])[0]["problems"][0])

    def test_commits_of_one_pull_request_are_reported_once(self):
        api = repo_with([review("bob", "APPROVED")])
        api.responses[f"repos/{REPO}/commits/c2/pulls"] = api.responses[f"repos/{REPO}/commits/m1/pulls"]
        self.assertEqual(len(audit.audit(api, ["c2", "m1"])), 1)

    def test_http_error_message_hides_token_and_closes_response(self):
        error = HTTPError("https://api.github.com/x", 404, "Not Found", {}, io.BytesIO())
        with patch.object(audit, "urlopen", side_effect=error):
            with self.assertRaisesRegex(audit.AuditError, "HTTP 404") as caught:
                audit.GitHub("secret-test-token").get(f"repos/{REPO}/pulls/1?per_page=1")
        self.assertNotIn("secret-test-token", str(caught.exception))
        self.assertTrue(error.closed)

    def test_push_event_writes_summary_and_stays_green(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            summary = Path(directory) / "summary.md"
            event.write_text(json.dumps({"repository": {"full_name": REPO}, "after": "m1",
                                         "commits": [{"id": "m1"}]}), encoding="utf-8")
            env = {"GITHUB_EVENT_PATH": str(event), "GITHUB_STEP_SUMMARY": str(summary)}
            with patch("sys.stdout", new_callable=io.StringIO) as out:
                code = audit.main([], api=repo_with([review("alice", "APPROVED")]), environ=env)
            self.assertEqual(code, 0)
            text = summary.read_text(encoding="utf-8")
            self.assertIn("1 of 1 audited merges need attention", text)
            self.assertIn("::warning title=Merge audit::PR #7", out.getvalue())
            with patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(audit.main(["--strict"], api=repo_with([review("alice", "APPROVED")]),
                                            environ=env), 1)

    def test_full_push_payload_is_reported_as_possibly_incomplete(self):
        shas, note = audit.push_shas({"after": "c19", "commits": [{"id": f"c{i}"} for i in range(20)]})
        self.assertEqual(len(shas), 20)
        self.assertIn("may not be audited", note)

    def test_other_repository_event_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"repository": {"full_name": "someone/fork"}}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                audit.main([], api=FakeAPI({}), environ={"GITHUB_EVENT_PATH": str(event)})

    def test_workflow_is_read_only_and_runs_on_main_pushes(self):
        workflow = (ROOT / ".github/workflows/merge-audit.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [main]", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertNotIn(": write", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertIn("persist-credentials: false", workflow)


if __name__ == "__main__":
    unittest.main()
