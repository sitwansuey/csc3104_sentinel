"""Project automation tests use fake APIs and cannot change the real board."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location(
    "project_status", Path(__file__).resolve().parents[1] / ".github/scripts/project_status.py")
automation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(automation)


class ProjectStatusTests(unittest.TestCase):
    def test_branch_requires_explicit_issue_number(self):
        self.assertEqual(automation.branch_issue("feat/21-live-health"), 21)
        self.assertEqual(automation.branch_issue("feature/9-verifier"), 9)
        for branch in ("main", "chore/project-status-automation", "feat/verifier-21", "feat/0-invalid"):
            self.assertIsNone(automation.branch_issue(branch))

    def test_first_non_merge_commit_uses_singapore_date(self):
        comparison = {"total_commits": 3, "commits": [
            {"parents": [{}, {}]},
            {"parents": [{}], "commit": {"committer": {"date": "2026-10-25T18:00:00Z"}}},
            {"parents": [{}], "commit": {"committer": {"date": "2026-10-27T00:00:00Z"}}},
        ]}
        self.assertEqual(automation.first_work_date(comparison), "2026-10-26")

    def test_branch_creation_and_merge_only_are_not_implementation(self):
        self.assertIsNone(automation.first_work_date({"total_commits": 0, "commits": []}))
        self.assertIsNone(automation.first_work_date({"total_commits": 1, "commits": [{"parents": [{}, {}]}]}))

    def test_truncated_history_does_not_invent_start_date(self):
        with self.assertRaises(ValueError):
            automation.first_work_date({"total_commits": 251, "commits": []})

    def test_first_push_updates_todo_and_empty_start_date(self):
        api = Mock()
        automation.update_item(api, {"id": "item", "status": {"name": "Todo"}, "start": None}, date="2026-10-26")
        self.assertEqual(api.update.call_count, 2)
        api.update.assert_any_call("item", automation.STATUS_FIELD, {"singleSelectOptionId": automation.IN_PROGRESS})
        api.update.assert_any_call("item", automation.START_FIELD, {"date": "2026-10-26"})

    def test_later_push_preserves_start_and_ready_status(self):
        api = Mock()
        automation.update_item(api, {"id": "item", "status": {"name": "Ready To Merge"},
                                     "start": {"date": "2026-10-24"}}, date="2026-10-26")
        api.update.assert_not_called()

    def test_push_does_not_move_backlog_or_done(self):
        for status in ("Backlog", "Done"):
            api = Mock()
            automation.update_item(api, {"id": "item", "status": {"name": status}}, date="2026-10-26")
            api.update.assert_not_called()

    def test_ready_label_moves_active_card_without_setting_start(self):
        api = Mock()
        automation.update_item(api, {"id": "item", "status": {"name": "In Progress"}}, ready=True)
        api.update.assert_called_once_with("item", automation.STATUS_FIELD,
                                          {"singleSelectOptionId": automation.READY})

    def test_closed_issue_push_is_ignored(self):
        api = Mock()
        api.issue.return_value = {"id": "issue", "state": "CLOSED"}
        automation.handle_push(api, {"workflow_run": {"event": "push", "head_branch": "feat/9-verifier",
                                                       "repository": {"full_name": automation.REPOSITORY}}})
        api.items.assert_not_called()
        api.request.assert_not_called()

    def test_push_reads_exact_head_and_updates_existing_issue(self):
        api = Mock()
        api.issue.return_value = {"id": "issue", "state": "OPEN"}
        api.request.side_effect = [
            {"total_commits": 1, "commits": [
                {"parents": [{}], "commit": {"committer": {"date": "2026-10-25T18:00:00Z"}}}]},
            [],
        ]
        api.items.return_value = {"issue": {"id": "item", "status": {"name": "Todo"}, "start": None}}
        automation.handle_push(api, {
            "repository": {"default_branch": "main"},
            "workflow_run": {"event": "push", "head_branch": "feat/21-verifier", "head_sha": "abc123",
                             "repository": {"full_name": automation.REPOSITORY}},
        })
        api.request.assert_any_call(f"repos/{automation.REPOSITORY}/compare/main...abc123")
        api.update.assert_any_call("item", automation.START_FIELD, {"date": "2026-10-26"})

    def test_push_reconciles_ready_pr_when_events_are_coalesced(self):
        api = Mock()
        api.issue.return_value = {"id": "issue", "state": "OPEN"}
        api.request.side_effect = [
            {"total_commits": 1, "commits": [
                {"parents": [{}], "commit": {"committer": {"date": "2026-10-26T01:00:00Z"}}}]},
            [{"number": 31}],
        ]
        api.graphql.return_value = {"repository": {"pullRequest": self.pull()}}
        api.items.return_value = {"issue": {"id": "item", "status": {"name": "In Progress"},
                                            "start": {"date": "2026-10-26"}}}
        automation.handle_push(api, {
            "repository": {"default_branch": "main"},
            "workflow_run": {"event": "push", "head_branch": "feat/21-verifier", "head_sha": "abc123",
                             "repository": {"full_name": automation.REPOSITORY}},
        })
        api.update.assert_called_once_with("item", automation.STATUS_FIELD,
                                          {"singleSelectOptionId": automation.READY})

    def pull(self, **changes):
        result = {"id": "pr", "state": "OPEN", "isDraft": False, "headRefName": "feat/21-verifier",
                  "labels": {"nodes": [{"name": "ready to merge"}]},
                  "closingIssuesReferences": {"nodes": [
                      {"id": "issue", "number": 21, "state": "OPEN",
                       "repository": {"nameWithOwner": automation.REPOSITORY}},
                      {"id": "external", "number": 3, "state": "OPEN",
                       "repository": {"nameWithOwner": "someone/else"}}],
                      "pageInfo": {"hasNextPage": False}}}
        result.update(changes)
        return result

    def test_pr_updates_linked_issue_without_creating_pr_card(self):
        api = Mock()
        api.graphql.return_value = {"repository": {"pullRequest": self.pull()}}
        api.issue.return_value = {"id": "issue", "number": 21, "state": "OPEN"}
        api.items.return_value = {"issue": {"id": "item", "status": {"name": "In Progress"}}}
        automation.handle_pr(api, {"pull_request": {"number": 31}})
        api.update.assert_called_once_with("item", automation.STATUS_FIELD,
                                          {"singleSelectOptionId": automation.READY})

    def test_existing_pr_card_is_updated_too(self):
        api = Mock()
        api.graphql.return_value = {"repository": {"pullRequest": self.pull()}}
        api.issue.return_value = {"id": "issue", "number": 21, "state": "OPEN"}
        api.items.return_value = {
            "issue": {"id": "item", "status": {"name": "In Progress"}},
            "pr": {"id": "pr-item", "status": {"name": "Todo"}}}
        automation.handle_pr(api, {"pull_request": {"number": 31}})
        self.assertEqual(api.update.call_count, 2)

    def test_draft_closed_and_removed_label_events_cannot_mark_ready(self):
        for changes in ({"isDraft": True}, {"state": "MERGED"}, {"labels": {"nodes": []}}):
            api = Mock()
            api.graphql.return_value = {"repository": {"pullRequest": self.pull(**changes)}}
            automation.handle_pr(api, {"pull_request": {"number": 31}})
            api.items.assert_not_called()
            api.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
