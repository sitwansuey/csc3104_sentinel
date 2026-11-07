"""First-work automation tests never write to the real project."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
import io

spec = importlib.util.spec_from_file_location(
    "project_status", Path(__file__).resolve().parents[2] / ".github/scripts/project_status.py")
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def card(status="Todo", branch="anything-you-like"):
    return {"id": "card", "content": {"id": "issue", "number": 21, "state": "OPEN",
            "linkedBranches": {"nodes": [{"ref": {"name": branch, "repository": {"nameWithOwner": a.REPOSITORY}}}],
                               "pageInfo": {"hasNextPage": False}}},
            "status": {"name": status}, "start": None, "automation": None}


def work():
    return {"total_commits": 1, "commits": [
        {"parents": [{}], "commit": {"committer": {"date": "2026-10-25T18:00:00Z"}}}]}


def event(branch="anything-you-like"):
    return {"repository": {"default_branch": "main"},
            "workflow_run": {"event": "push", "head_branch": branch, "head_sha": "abc",
                             "repository": {"full_name": a.REPOSITORY}}}


class ProjectStartTests(unittest.TestCase):
    def test_repository_reads_use_workflow_token_and_project_mutations_use_project_token(self):
        api = a.GitHub("project-test-token", "repository-test-token")
        response = Mock()
        response.__enter__ = Mock(return_value=io.StringIO('{"ok":true}'))
        response.__exit__ = Mock(return_value=False)
        with patch.object(a, 'urlopen', return_value=response) as request:
            api.request('repos/' + a.REPOSITORY + '/compare/main...abc')
            self.assertEqual(request.call_args.args[0].get_header('Authorization'),
                             'Bearer repository-test-token')
        response.__enter__ = Mock(return_value=io.StringIO('{"data":{"ok":true}}'))
        with patch.object(a, 'urlopen', return_value=response) as request:
            api.update('card', a.STATUS_FIELD, {'singleSelectOptionId': a.IN_PROGRESS})
            self.assertEqual(request.call_args.args[0].get_header('Authorization'),
                             'Bearer project-test-token')

    def test_closing_issue_graphql_uses_repository_credentials(self):
        api = a.GitHub('project-test-token', 'repository-test-token')
        api.request = Mock(return_value=[{'number': 40}])
        api.graphql = Mock(side_effect=AssertionError('Project token used for repository query'))
        api.repository_api.graphql = Mock(return_value={'repository': {'pullRequest': {
            'closingIssuesReferences': {'nodes': [{'id':'issue', 'repository': {'nameWithOwner': a.REPOSITORY}}],
                                       'pageInfo': {'hasNextPage':False}}}}})
        self.assertEqual(api.pull_issues('arbitrary-branch'), {'issue'})
        api.repository_api.graphql.assert_called_once()

    def test_http_failure_names_token_role_without_exposing_credentials_and_closes_response(self):
        api = a.GitHub('project-test-token', 'repository-test-token')
        error = HTTPError('https://api.github.com/test', 404, 'Not Found', {}, io.BytesIO())
        with patch.object(a, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, 'HTTP 404 using GITHUB_TOKEN') as caught:
                api.request('repos/' + a.REPOSITORY + '/compare/main...abc')
        self.assertNotIn('repository-test-token', str(caught.exception))
        self.assertTrue(error.closed)

    def test_workflow_supplies_read_only_repository_token_and_trusted_default_branch(self):
        workflow = (Path(__file__).parents[2] / '.github/workflows/project-status.yml').read_text()
        self.assertIn('GITHUB_TOKEN: ${{ github.token }}', workflow)
        self.assertIn('contents: read', workflow)
        self.assertIn('pull-requests: read', workflow)
        self.assertIn('ref: ${{ github.event.repository.default_branch }}', workflow)
        self.assertNotIn('pull_request_target:', workflow)

    def test_arbitrary_branch_name_is_linked_without_parsing_messages(self):
        self.assertTrue(a.linked_to_branch(card(), "anything-you-like"))
        self.assertFalse(a.linked_to_branch(card(), "feat/21-guessed-name"))

    def test_deleted_or_other_repo_branch_is_not_matched(self):
        item = card()
        item["content"]["linkedBranches"]["nodes"] = [{"ref": None}]
        self.assertFalse(a.linked_to_branch(item, "anything-you-like"))
        item = card()
        item["content"]["linkedBranches"]["nodes"][0]["ref"]["repository"]["nameWithOwner"] = "other/repo"
        self.assertFalse(a.linked_to_branch(item, "anything-you-like"))

    def test_todo_and_backlog_start_with_date_and_marker(self):
        for status in ("Todo", "Backlog"):
            api = Mock()
            a.start_once(api, card(status), "2026-10-26")
            api.update.assert_any_call("card", a.STATUS_FIELD, {"singleSelectOptionId": a.IN_PROGRESS})
            api.update.assert_any_call("card", a.START_FIELD, {"date": "2026-10-26"})
            self.assertEqual(api.update.call_args_list[-1].args[1], a.AUTOMATION_FIELD)

    def test_later_push_preserves_manual_queue_change(self):
        for status in ("Todo", "Backlog", "Ready To Merge", "Will Not Implement"):
            item = card(status)
            item["automation"] = {"text": '{"work_started":true}'}
            api = Mock()
            a.start_once(api, item, "2026-10-27")
            api.update.assert_not_called()

    def test_historical_start_is_not_overwritten(self):
        item = card()
        item["start"] = {"date": "2026-10-20"}
        api = Mock()
        a.start_once(api, item, "2026-10-26")
        self.assertFalse(any(call.args[1] == a.START_FIELD for call in api.update.call_args_list))

    def test_closed_issue_is_ignored(self):
        item = card()
        item["content"]["state"] = "CLOSED"
        api = Mock()
        a.start_once(api, item, "2026-10-26")
        api.update.assert_not_called()

    def test_advanced_status_is_not_moved_backwards(self):
        for status in ("In Progress", "Ready To Merge", "Done", "Will Not Implement"):
            api = Mock()
            a.start_once(api, card(status), "2026-10-26")
            self.assertFalse(any(call.args[1] == a.STATUS_FIELD for call in api.update.call_args_list))

    def test_failed_update_does_not_persist_started_marker(self):
        api = Mock()
        api.update.side_effect = RuntimeError("temporary API error")
        with self.assertRaises(RuntimeError):
            a.start_once(api, card(), "2026-10-26")
        self.assertFalse(any(call.args[1] == a.AUTOMATION_FIELD for call in api.update.call_args_list))

    def test_legacy_label_state_is_replaced(self):
        item = card()
        item["automation"] = {"text": json.dumps({"queued": "Todo", "label_controlled": True, "pr": 31})}
        api = Mock()
        a.start_once(api, item, "2026-10-26")
        api.update.assert_any_call("card", a.AUTOMATION_FIELD, {"text": '{"work_started":true}'})

    def test_branch_creation_and_merge_only_do_not_start_work(self):
        self.assertIsNone(a.first_work_date({"total_commits": 0, "commits": []}))
        self.assertIsNone(a.first_work_date({"total_commits": 1, "commits": [{"parents": [{}, {}]}]}))
        api = Mock()
        api.request.return_value = {"total_commits": 0, "commits": []}
        a.handle_push(api, event())
        api.items.assert_not_called()

    def test_date_is_singapore_and_truncated_history_fails(self):
        self.assertEqual(a.first_work_date(work()), "2026-10-26")
        with self.assertRaises(ValueError):
            a.first_work_date({"total_commits": 251, "commits": []})

    def test_push_uses_explicit_issue_link(self):
        api = Mock()
        api.request.return_value = work()
        api.items.return_value = {"issue": card()}
        api.pull_issues.return_value = set()
        a.handle_push(api, event())
        api.update.assert_any_call("card", a.STATUS_FIELD, {"singleSelectOptionId": a.IN_PROGRESS})

    def test_open_pr_link_replaces_branch_link(self):
        item = card(branch="different")
        api = Mock()
        api.request.return_value = work()
        api.items.return_value = {"issue": item}
        api.pull_issues.return_value = {"issue"}
        a.handle_push(api, event())
        api.update.assert_any_call("card", a.STATUS_FIELD, {"singleSelectOptionId": a.IN_PROGRESS})

    def test_unlinked_branch_does_not_guess_issue_number(self):
        api = Mock()
        api.request.return_value = work()
        api.items.return_value = {"issue": card(branch="different")}
        api.pull_issues.return_value = set()
        a.handle_push(api, event("feat/21-regex-no-longer-used"))
        api.update.assert_not_called()

    def test_main_and_non_push_are_ignored(self):
        for payload in (event("main"), event()):
            if payload["workflow_run"]["head_branch"] != "main":
                payload["workflow_run"]["event"] = "pull_request"
            api = Mock()
            a.handle_push(api, payload)
            api.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
