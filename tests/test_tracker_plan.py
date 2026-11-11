"""Tracker-plan tests use a fake gh runner; nothing reaches GitHub."""
import copy
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from scripts.tracker import plan as tracker

REPO = "example/repo"


class FakeGh:
    """Serves GET paths from a dict (lists are paged); records every call."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, args, payload=None):
        self.calls.append((list(args), payload))
        method = args[args.index("--method") + 1]
        path = args[args.index("--method") + 2]
        if method == "POST" and path.endswith("/milestones"):
            # Like GitHub, a created milestone is listed afterwards.
            created = {"number": 90 + len(self.calls), "title": payload["title"]}
            self.responses[path].append(created)
            return json.dumps(created)
        if method != "GET":
            return json.dumps({"ok": True})
        base, _, query = path.partition("?")
        if base not in self.responses:
            raise tracker.PlanError(f"gh api --method GET failed: HTTP 404 for {base}")
        value = self.responses[base]
        if isinstance(value, list):
            page = int(re.search(r"(?:^|&)page=(\d+)", query).group(1))
            return json.dumps(value[(page - 1) * tracker.PAGE:page * tracker.PAGE])
        return json.dumps(value)

    def writes(self):
        return [(args[args.index("--method") + 1], args[args.index("--method") + 2], payload)
                for args, payload in self.calls if args[args.index("--method") + 1] != "GET"]


def small_plan():
    return {
        "schema": tracker.SCHEMA, "repository": REPO,
        "snapshot": {"taken": "2026-11-12", "open_issues": [10, 14]},
        "milestones": [{"id": "milestone-new", "op": "create", "title": "6. Later", "description": "After."}],
        "new_issues": [{"id": "new-defect", "title": "[X] A defect", "body": "Details.", "labels": ["bug"],
                        "milestone": "6. Later"}],
        "issues": [
            {"number": 10, "roadmap": ["P2.02"], "actions": [
                {"id": "i10-label", "op": "remove_label", "label": "not started"},
                {"id": "i10-note", "op": "comment", "body": "Short factual note."},
                {"id": "i10-milestone", "op": "set_milestone", "milestone": "6. Later"}]},
            {"number": 14, "roadmap": [], "roadmap_note": "none", "actions": [
                {"id": "i14-close", "op": "close", "reason": "completed", "signed_off_by": None}]},
        ],
        "closed_issues": [{"number": 7, "actions": [{"id": "c7-link", "op": "comment", "body": "Moved doc."}]}],
        "pulls": [{"number": 88, "actions": [{"id": "p88-note", "op": "comment", "body": "Superseded."}]}],
        "decisions_pending": ["Pick a governance option."],
    }


def github_state(**overrides):
    state = {
        f"repos/{REPO}/milestones": [{"number": 1, "title": "1. First", "description": ""}],
        f"repos/{REPO}/issues/10": {"state": "open", "labels": [{"name": "not started"}], "milestone": None},
        f"repos/{REPO}/issues/10/comments": [],
        f"repos/{REPO}/issues/14": {"state": "open", "labels": [], "milestone": None},
        f"repos/{REPO}/issues/14/comments": [],
        f"repos/{REPO}/issues/7": {"state": "closed", "labels": [], "milestone": None},
        f"repos/{REPO}/issues/7/comments": [],
        f"repos/{REPO}/issues/88": {"state": "open", "labels": [], "milestone": None},
        f"repos/{REPO}/issues/88/comments": [],
        f"repos/{REPO}/issues": [{"number": 10, "body": "old"}],
    }
    state.update(overrides)
    return state


class TrackerPlanTests(unittest.TestCase):
    def run_plan(self, plan, argv, responses=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            path.write_text(json.dumps(plan), encoding="utf-8")
            fake = FakeGh(github_state() if responses is None else responses)
            out, err = io.StringIO(), io.StringIO()
            with patch("sys.stderr", err):
                code = tracker.main(["--plan", str(path), *argv], runner=fake, out=out)
        return code, out.getvalue(), err.getvalue(), fake

    def test_dry_run_prints_exact_operations_and_makes_no_writes(self):
        code, out, _, fake = self.run_plan(small_plan(), [])
        self.assertEqual(code, 0)
        self.assertEqual(fake.writes(), [])
        self.assertTrue(all("GET" in args for args, _ in fake.calls))
        self.assertIn("Mode: DRY RUN. Nothing is written.", out)
        self.assertIn(f"DELETE repos/{REPO}/issues/10/labels/not%20started", out)
        self.assertIn(f"POST repos/{REPO}/issues/10/comments", out)
        self.assertIn("    | <!-- tracker-plan:i10-note -->", out)
        self.assertIn("[blocked] i14-close", out)
        self.assertIn("needs signed_off_by", out)
        self.assertIn("Summary: 7 pending, 1 blocked", out)
        self.assertIn("--apply --confirm-count 7", out)
        self.assertIn("Pick a governance option.", out)

    def test_no_fetch_validates_without_any_gh_call(self):
        code, out, _, fake = self.run_plan(small_plan(), ["--no-fetch"])
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        self.assertIn("without fetching state", out)

    def test_apply_without_confirm_count_refuses_before_any_call(self):
        code, _, err, fake = self.run_plan(small_plan(), ["--apply"])
        self.assertEqual(code, 2)
        self.assertIn("--apply refused", err)
        self.assertEqual(fake.calls, [])

    def test_apply_with_wrong_count_refuses_without_writes(self):
        for count in ("6", "8", "0"):
            code, _, err, fake = self.run_plan(small_plan(), ["--apply", "--confirm-count", count])
            self.assertEqual(code, 2)
            self.assertIn("7 operations are pending", err)
            self.assertEqual(fake.writes(), [])

    def test_apply_cannot_skip_the_state_fetch(self):
        code, _, err, fake = self.run_plan(small_plan(), ["--apply", "--confirm-count", "7", "--no-fetch"])
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])

    def test_apply_with_matching_count_writes_in_order_and_skips_blocked(self):
        responses = github_state()
        code, out, _, fake = self.run_plan(small_plan(), ["--apply", "--confirm-count", "7"], responses)
        self.assertEqual(code, 0, out)
        writes = fake.writes()
        self.assertEqual([(method, path) for method, path, _ in writes], [
            ("POST", f"repos/{REPO}/milestones"),
            ("POST", f"repos/{REPO}/issues"),
            ("DELETE", f"repos/{REPO}/issues/10/labels/not%20started"),
            ("POST", f"repos/{REPO}/issues/10/comments"),
            ("PATCH", f"repos/{REPO}/issues/10"),
            ("POST", f"repos/{REPO}/issues/7/comments"),
            ("POST", f"repos/{REPO}/issues/88/comments"),
        ])
        self.assertNotIn(f"repos/{REPO}/issues/14", [path for _, path, _ in writes])
        self.assertTrue(writes[3][2]["body"].endswith("<!-- tracker-plan:i10-note -->"))
        self.assertTrue(writes[1][2]["body"].endswith("<!-- tracker-plan:new-defect -->"))
        self.assertIn("applied i10-note", out)

    def test_apply_resolves_a_milestone_title_to_its_number(self):
        responses = github_state()
        fake = FakeGh(responses)
        plan = small_plan()
        tracker.validate(plan)
        api = tracker.GitHub(REPO, fake)
        ops = tracker.operations(plan, tracker.fetch_state(api, plan))
        tracker.apply(api, plan, ops, io.StringIO())
        created = next(m["number"] for m in responses[f"repos/{REPO}/milestones"] if m["title"] == "6. Later")
        payloads = {path: payload for _, path, payload in fake.writes()}
        self.assertEqual(payloads[f"repos/{REPO}/issues/10"], {"milestone": created})
        self.assertEqual(payloads[f"repos/{REPO}/issues"]["milestone"], created)

    def test_apply_stops_when_a_milestone_cannot_be_resolved(self):
        # The milestone was deleted between the dry run and the apply.
        fake = FakeGh(github_state(**{f"repos/{REPO}/milestones": []}))
        op = {"id": "i10-milestone", "label": "#10 set milestone", "method": "PATCH", "status": "pending",
              "path": f"repos/{REPO}/issues/10", "payload": {"milestone": "6. Later"}}
        with self.assertRaisesRegex(tracker.PlanError, "not found"):
            tracker.apply(tracker.GitHub(REPO, fake), small_plan(), [op], io.StringIO())
        self.assertEqual(fake.writes(), [])

    def test_markers_and_current_state_make_a_rerun_idempotent(self):
        responses = github_state(**{
            f"repos/{REPO}/milestones": [{"number": 6, "title": "6. Later", "description": "After."}],
            f"repos/{REPO}/issues/10": {"state": "open", "labels": [], "milestone": {"title": "6. Later"}},
            f"repos/{REPO}/issues/10/comments": [{"body": "x\n\n<!-- tracker-plan:i10-note -->"}],
            f"repos/{REPO}/issues/7/comments": [{"body": "<!-- tracker-plan:c7-link -->"}],
            f"repos/{REPO}/issues/88/comments": [{"body": "<!-- tracker-plan:p88-note -->"}],
            f"repos/{REPO}/issues": [{"number": 101, "body": "Details.\n\n<!-- tracker-plan:new-defect -->"}],
        })
        code, out, _, fake = self.run_plan(small_plan(), [], responses)
        self.assertEqual(code, 0)
        self.assertIn("Summary: 0 pending, 1 blocked (need sign-off), 7 already done", out)
        code, _, err, fake = self.run_plan(small_plan(), ["--apply", "--confirm-count", "0"], responses)
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.writes(), [])

    def test_signed_off_closure_becomes_pending(self):
        plan = small_plan()
        plan["issues"][1]["actions"][0]["signed_off_by"] = "API owner, 12 Nov"
        code, out, _, _ = self.run_plan(plan, [])
        self.assertIn("[pending] i14-close", out)
        self.assertIn('{"state": "closed", "state_reason": "completed"}', out)

    def test_needs_sign_off_blocks_any_action(self):
        plan = small_plan()
        plan["issues"][0]["actions"][2]["needs_sign_off"] = True
        _, out, _, _ = self.run_plan(plan, [])
        self.assertIn("[blocked] i10-milestone", out)

    def test_issue_closed_since_snapshot_is_left_alone(self):
        responses = github_state(**{f"repos/{REPO}/issues/10": {"state": "closed", "labels": [], "milestone": None}})
        _, out, _, _ = self.run_plan(small_plan(), [], responses)
        self.assertIn("i10-note: #10 comment (skipped: #10 is closed now)", out)
        self.assertNotIn(f"POST repos/{REPO}/issues/10/comments", out)

    def test_unexpected_milestone_title_fails_closed(self):
        plan = small_plan()
        plan["milestones"].append({"id": "milestone-edit", "op": "edit", "number": 1, "expect_title": "1. Old",
                                   "title": "1. New", "description": "Renamed."})
        code, _, err, fake = self.run_plan(plan, [])
        self.assertEqual(code, 2)
        self.assertIn('milestone 1 is now "1. First"', err)
        self.assertEqual(fake.writes(), [])

    def test_unknown_milestone_fails_closed(self):
        plan = small_plan()
        plan["issues"][0]["actions"][2]["milestone"] = "9. Missing"
        code, _, err, _ = self.run_plan(plan, [])
        self.assertEqual(code, 2)
        self.assertIn("neither exists nor is created by the plan", err)

    def test_api_failure_or_truncation_fails_closed(self):
        responses = github_state()
        del responses[f"repos/{REPO}/issues/10/comments"]
        code, out, err, fake = self.run_plan(small_plan(), [], responses)
        self.assertEqual(code, 2)
        self.assertIn("HTTP 404", err)
        self.assertEqual(out, "")
        responses = github_state(**{f"repos/{REPO}/issues/10/comments": [{"body": ""}] * tracker.PAGE})
        with patch.object(tracker, "MAX_PAGES", 1):
            code, _, err, _ = self.run_plan(small_plan(), [], responses)
        self.assertEqual(code, 2)
        self.assertIn("refusing to guess", err)

    def test_invalid_plans_are_rejected(self):
        cases = {
            "unknown op": lambda p: p["issues"][0]["actions"].append({"id": "i10-x", "op": "delete"}),
            "repeated action ids": lambda p: p["issues"][0]["actions"].append(
                {"id": "i10-note", "op": "comment", "body": "Again."}),
            "without a plan entry": lambda p: p["snapshot"]["open_issues"].append(99),
            "keep it short": lambda p: p["issues"][0]["actions"][1].update(body="x" * (tracker.MAX_COMMENT + 1)),
            "close reason": lambda p: p["issues"][1]["actions"][0].update(reason="wontfix"),
            "both added and removed": lambda p: p["issues"][0]["actions"].append(
                {"id": "i10-add", "op": "add_label", "label": "not started"}),
            "own marker": lambda p: p["issues"][0]["actions"][1].update(body="<!-- tracker-plan:x -->"),
            "roadmap": lambda p: p["issues"][0].update(roadmap=["2.02"]),
            "bad action id": lambda p: p["issues"][0]["actions"][1].update(id="Note 10"),
            "op must be one of": lambda p: p["closed_issues"][0]["actions"].append(
                {"id": "c7-close", "op": "close", "reason": "completed"}),
            "schema": lambda p: p.update(schema="other"),
        }
        for message, change in cases.items():
            plan = copy.deepcopy(small_plan())
            change(plan)
            with self.assertRaisesRegex(tracker.PlanError, message):
                tracker.validate(plan)

    def test_committed_plan_is_valid_reviewed_data(self):
        plan = tracker.load_plan(tracker.PLAN)
        self.assertEqual(len(plan["snapshot"]["open_issues"]), 33)
        self.assertEqual({entry["number"] for entry in plan["issues"]}, set(plan["snapshot"]["open_issues"]))
        text = tracker.PLAN.read_text(encoding="utf-8")
        for forbidden in ("C:\\", "/Users/", "/home/", "AppData"):
            self.assertNotIn(forbidden, text)
        ops = tracker.operations(plan)
        closes = [op for op in ops if op["method"] == "PATCH" and op["payload"].get("state") == "closed"]
        self.assertTrue(closes)
        # No closure is signed off by this branch; owners add signed_off_by after review.
        self.assertTrue(all(op["status"] == "blocked" for op in closes))
        dead_links = [entry for entry in plan["closed_issues"]]
        self.assertEqual(len(dead_links), 12)
        for entry in dead_links:
            self.assertIn("/blob/a90d56a3dda322bcadfa6f6ec163fea78ee167fb/docs/repository-review-2026-11-05.md",
                          entry["actions"][0]["body"])


if __name__ == "__main__":
    unittest.main()
