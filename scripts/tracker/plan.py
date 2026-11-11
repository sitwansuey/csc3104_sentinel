"""Print, and only on explicit confirmation apply, a reviewed issue-tracker plan.

The plan (scripts/tracker/tracker-plan.json) lists per-issue actions: label
changes, short comments, milestone moves, closures and new issues. By default
this is a dry run. It reads the current state with read-only `gh api` GETs and
prints every write still pending, with its exact method, path and payload.
Comments and new issues carry a hidden marker, so a rerun skips work already
done. A closure, or any action marked needs_sign_off, stays blocked until the
plan names who signed it off.

Writing needs --apply AND --confirm-count N, where N is the number of pending
operations the dry run printed. Anything else refuses before the first write.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
PLAN = Path(__file__).resolve().with_name("tracker-plan.json")
SCHEMA = "sentinel-tracker-plan/1"
PAGE = 100
MAX_PAGES = 20
MAX_COMMENT = 900
MAX_ISSUE_BODY = 3000
ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
ROADMAP_ID = re.compile(r"^P[1-5]\.\d\d$")
MARKER = re.compile(r"<!-- tracker-plan:([a-z0-9-]+) -->")
ISSUE_OPS = {"comment", "add_label", "remove_label", "set_milestone", "close"}
PULL_OPS = {"comment", "close"}
CLOSE_REASONS = {"completed", "not_planned"}


class PlanError(ValueError):
    """The plan or the fetched state cannot be trusted; nothing is written."""


def marker(action_id):
    return f"<!-- tracker-plan:{action_id} -->"


def _require(condition, message):
    if not condition:
        raise PlanError(message)


def _text(value, name, limit):
    _require(isinstance(value, str) and value.strip(), f"{name} must be non-empty text")
    _require(len(value) <= limit, f"{name} is longer than {limit} characters; keep it short")
    _require("tracker-plan:" not in value, f"{name} must not contain its own marker")


def validate(plan):
    """Check the plan's shape before any request; return the list of action ids."""
    _require(isinstance(plan, dict) and plan.get("schema") == SCHEMA, f"plan schema must be {SCHEMA}")
    _require(re.fullmatch(r"[\w.-]+/[\w.-]+", str(plan.get("repository", ""))), "repository must be owner/name")
    snapshot = plan.get("snapshot") or {}
    open_issues = snapshot.get("open_issues")
    _require(isinstance(open_issues, list) and all(type(n) is int for n in open_issues),
             "snapshot.open_issues must list issue numbers")
    for key in ("issues", "closed_issues", "pulls", "milestones", "new_issues", "decisions_pending"):
        _require(isinstance(plan.get(key, []), list), f"{key} must be a list")
    ids = []
    created_milestones = set()
    for item in plan.get("milestones", []):
        ids.append(item.get("id"))
        _require(item.get("op") in {"create", "edit"}, f"milestone {item.get('id')}: op must be create or edit")
        _text(item.get("title"), f"milestone {item.get('id')} title", 80)
        _text(item.get("description"), f"milestone {item.get('id')} description", 400)
        if item["op"] == "edit":
            _require(type(item.get("number")) is int, f"milestone {item['id']}: edit needs a number")
            _text(item.get("expect_title"), f"milestone {item['id']} expect_title", 80)
        created_milestones.add(item["title"])
    numbers = set()
    for entry in plan.get("issues", []):
        number = entry.get("number")
        _require(type(number) is int and number not in numbers, f"issue entry {number!r} is missing or repeated")
        numbers.add(number)
        roadmap = entry.get("roadmap")
        _require(isinstance(roadmap, list) and all(ROADMAP_ID.match(str(r)) for r in roadmap),
                 f"#{number}: roadmap must list item ids such as P1.13")
        _require(roadmap or entry.get("roadmap_note"), f"#{number}: give roadmap ids or a roadmap_note")
        labels_added, labels_removed = set(), set()
        for action in entry.get("actions", []):
            ids.append(action.get("id"))
            op = action.get("op")
            _require(op in ISSUE_OPS, f"#{number} {action.get('id')}: unknown op {op!r}")
            if op == "comment":
                _text(action.get("body"), f"#{number} {action['id']} body", MAX_COMMENT)
            elif op in {"add_label", "remove_label"}:
                _text(action.get("label"), f"#{number} {action['id']} label", 50)
                (labels_added if op == "add_label" else labels_removed).add(action["label"])
            elif op == "set_milestone":
                _text(action.get("milestone"), f"#{number} {action['id']} milestone", 80)
            elif op == "close":
                _require(action.get("reason") in CLOSE_REASONS, f"#{number} {action['id']}: close reason must be "
                         "completed or not_planned")
        _require(not labels_added & labels_removed, f"#{number}: a label is both added and removed")
    missing = sorted(set(open_issues) - numbers)
    _require(not missing, "open issues without a plan entry: " + ", ".join(f"#{n}" for n in missing))
    for key, allowed in (("closed_issues", {"comment"}), ("pulls", PULL_OPS)):
        for entry in plan.get(key, []):
            number = entry.get("number")
            _require(type(number) is int and number not in numbers, f"{key} entry {number!r} is missing or repeated")
            numbers.add(number)
            for action in entry.get("actions", []):
                ids.append(action.get("id"))
                _require(action.get("op") in allowed, f"{key} #{number} {action.get('id')}: op must be one of "
                         + ", ".join(sorted(allowed)))
                if action["op"] == "comment":
                    _text(action.get("body"), f"#{number} {action['id']} body", MAX_COMMENT)
    for item in plan.get("new_issues", []):
        ids.append(item.get("id"))
        _text(item.get("title"), f"new issue {item.get('id')} title", 120)
        _text(item.get("body"), f"new issue {item.get('id')} body", MAX_ISSUE_BODY)
        _require(isinstance(item.get("labels", []), list), f"new issue {item['id']}: labels must be a list")
    for value in plan.get("decisions_pending", []):
        _text(value, "decision", 600)
    for action_id in ids:
        _require(isinstance(action_id, str) and ID.match(action_id), f"bad action id {action_id!r}")
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    _require(not duplicates, "repeated action ids: " + ", ".join(duplicates))
    return ids


def load_plan(path):
    try:
        plan = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PlanError(f"cannot read plan {path}: {error}") from None
    validate(plan)
    return plan


def gh(args, payload=None):
    """Run the gh CLI; a JSON payload goes on stdin."""
    result = subprocess.run(["gh", *args], input=None if payload is None else json.dumps(payload),
                            capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
    if result.returncode:
        raise PlanError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


class GitHub:
    def __init__(self, repository, runner=gh):
        self.repository = repository
        self.runner = runner

    def get(self, path):
        text = self.runner(["api", "--method", "GET", path])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise PlanError(f"unreadable response for {path}") from None

    def get_all(self, path):
        items = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            batch = self.get(f"{path}{joiner}per_page={PAGE}&page={page}")
            _require(isinstance(batch, list), f"unexpected response for {path}")
            items.extend(batch)
            if len(batch) < PAGE:
                return items
        raise PlanError(f"more than {PAGE * MAX_PAGES} items for {path}; refusing to guess")

    def write(self, method, path, payload=None):
        args = ["api", "--method", method, path]
        if payload is not None:
            args += ["--input", "-"]
        text = self.runner(args, payload)
        return json.loads(text) if text.strip() else {}


def fetch_state(api, plan):
    """Read-only snapshot of everything the plan touches."""
    repo = plan["repository"]
    state = {"items": {}, "milestones": {}, "markers": set()}
    for milestone in api.get_all(f"repos/{repo}/milestones?state=all"):
        state["milestones"][milestone["number"]] = {"title": milestone["title"],
                                                    "description": milestone.get("description") or ""}
    numbers = [entry["number"] for key in ("issues", "closed_issues", "pulls") for entry in plan.get(key, [])]
    for number in numbers:
        issue = api.get(f"repos/{repo}/issues/{number}")
        comments = api.get_all(f"repos/{repo}/issues/{number}/comments")
        state["items"][number] = {
            "state": issue["state"], "labels": {label["name"] for label in issue.get("labels", [])},
            "milestone": (issue.get("milestone") or {}).get("title"),
            "markers": {m for c in comments for m in MARKER.findall(c.get("body") or "")}}
    if plan.get("new_issues"):
        for issue in api.get_all(f"repos/{repo}/issues?state=all"):
            state["markers"].update(MARKER.findall(issue.get("body") or ""))
    return state


def _signed(action):
    return bool(str(action.get("signed_off_by") or "").strip())


def operations(plan, state=None):
    """Turn the plan into ordered operations, each pending, done or blocked."""
    repo = plan["repository"]
    ops = []

    def add(action_id, label, method, path, payload, done=False, blocked=None):
        status = "blocked" if blocked and not done else ("done" if done else "pending")
        ops.append({"id": action_id, "label": label, "method": method, "path": path,
                    "payload": payload, "status": status, "reason": blocked if status == "blocked" else None})

    titles = {} if state is None else {m["title"]: n for n, m in state["milestones"].items()}
    for item in plan.get("milestones", []):
        payload = {"title": item["title"], "description": item["description"]}
        if item["op"] == "create":
            add(item["id"], f"create milestone \"{item['title']}\"", "POST", f"repos/{repo}/milestones",
                payload, done=item["title"] in titles)
            continue
        current = None if state is None else state["milestones"].get(item["number"])
        if state is not None:
            _require(current is not None, f"milestone {item['number']} does not exist")
            _require(current["title"] in {item["expect_title"], item["title"]},
                     f"milestone {item['number']} is now \"{current['title']}\", not \"{item['expect_title']}\"")
        add(item["id"], f"edit milestone {item['number']}: \"{item['expect_title']}\" -> \"{item['title']}\"",
            "PATCH", f"repos/{repo}/milestones/{item['number']}", payload,
            done=current is not None and current == payload)
    planned_titles = {item["title"] for item in plan.get("milestones", [])}
    for item in plan.get("new_issues", []):
        payload = {"title": item["title"], "body": item["body"].rstrip() + "\n\n" + marker(item["id"]),
                   "labels": item.get("labels", [])}
        if item.get("milestone"):
            title = item["milestone"]
            if state is not None:
                _require(title in titles or title in planned_titles,
                         f"new issue {item['id']}: milestone \"{title}\" neither exists nor is created by the plan")
            payload["milestone"] = titles.get(title, title)
        add(item["id"], f"open issue \"{item['title']}\"", "POST", f"repos/{repo}/issues", payload,
            done=state is not None and item["id"] in state["markers"],
            blocked="needs signed_off_by in the plan" if item.get("needs_sign_off") and not _signed(item) else None)
    for kind, entries in (("issue", plan.get("issues", [])), ("closed", plan.get("closed_issues", [])),
                          ("pull", plan.get("pulls", []))):
        for entry in entries:
            number = entry["number"]
            name = f"PR #{number}" if kind == "pull" else f"#{number}"
            current = None if state is None else state["items"][number]
            if kind != "closed" and current is not None and current["state"] == "closed":
                # Closed since the snapshot: report it and change nothing until the plan is reviewed again.
                for action in entry.get("actions", []):
                    add(action["id"], f"{name} {action['op']} (skipped: {name} is closed now)", "-", "-", None,
                        done=True)
                continue
            for action in entry.get("actions", []):
                op = action["op"]
                needs = op == "close" or action.get("needs_sign_off")
                blocked = "needs signed_off_by in the plan" if needs and not _signed(action) else None
                if op == "comment":
                    add(action["id"], f"{name} comment", "POST", f"repos/{repo}/issues/{number}/comments",
                        {"body": action["body"].rstrip() + "\n\n" + marker(action["id"])},
                        done=current is not None and action["id"] in current["markers"], blocked=blocked)
                elif op == "add_label":
                    add(action["id"], f"{name} add label \"{action['label']}\"", "POST",
                        f"repos/{repo}/issues/{number}/labels", {"labels": [action["label"]]},
                        done=current is not None and action["label"] in current["labels"], blocked=blocked)
                elif op == "remove_label":
                    add(action["id"], f"{name} remove label \"{action['label']}\"", "DELETE",
                        f"repos/{repo}/issues/{number}/labels/{quote(action['label'], safe='')}", None,
                        done=current is not None and action["label"] not in current["labels"], blocked=blocked)
                elif op == "set_milestone":
                    title = action["milestone"]
                    if state is not None:
                        _require(title in titles or title in planned_titles,
                                 f"{name}: milestone \"{title}\" neither exists nor is created by the plan")
                    known = title in titles
                    add(action["id"], f"{name} set milestone \"{title}\""
                        + ("" if known else " (number looked up when applied)"), "PATCH",
                        f"repos/{repo}/issues/{number}", {"milestone": titles[title] if known else title},
                        done=current is not None and current["milestone"] == title, blocked=blocked)
                elif op == "close":
                    path = (f"repos/{repo}/issues/{number}" if kind == "issue" else f"repos/{repo}/pulls/{number}")
                    payload = {"state": "closed"}
                    if kind == "issue":
                        payload["state_reason"] = action["reason"]
                    add(action["id"], f"{name} close ({action['reason']})", "PATCH", path, payload,
                        blocked=blocked)
    return ops


MODES = {"dry-run": "Mode: DRY RUN. Nothing is written.",
         "offline": "Mode: DRY RUN without fetching state; every operation is listed as pending.",
         "apply": "Mode: APPLY. The pending operations below are written now, in this order."}


def render(plan, ops, *, mode, source):
    lines = [f"Tracker plan: {source} (snapshot {plan['snapshot'].get('taken', '?')}, "
             f"{len(plan['snapshot']['open_issues'])} open issues)",
             f"Repository: {plan['repository']}", MODES[mode], ""]
    for op in ops:
        lines.append(f"[{op['status']}] {op['id']}: {op['label']}")
        if op["status"] == "blocked":
            lines.append(f"    not applied: {op['reason']}")
        if op["status"] == "done" or op["method"] == "-":
            continue
        lines.append(f"    {op['method']} {op['path']}")
        payload = dict(op["payload"] or {})
        body = payload.pop("body", None)
        if payload:
            lines.append("    " + json.dumps(payload, ensure_ascii=False, sort_keys=True))
        if body is not None:
            lines.extend("    | " + line for line in body.splitlines())
    lines += ["", "Roadmap mapping (gap-remediation plan item ids):"]
    for entry in plan.get("issues", []):
        mapped = ", ".join(entry["roadmap"]) or "none"
        note = f" ({entry['roadmap_note']})" if entry.get("roadmap_note") else ""
        lines.append(f"  #{entry['number']}: {mapped}{note}")
    if plan.get("decisions_pending"):
        lines += ["", "Decisions for the team (not operations):"]
        lines += [f"  - {decision}" for decision in plan["decisions_pending"]]
    counts = {status: sum(op["status"] == status for op in ops) for status in ("pending", "blocked", "done")}
    lines += ["", f"Summary: {counts['pending']} pending, {counts['blocked']} blocked (need sign-off), "
              f"{counts['done']} already done or skipped."]
    if mode == "dry-run" and counts["pending"]:
        lines.append("After review, a maintainer may apply them with: "
                     f"python scripts/tracker/plan.py --apply --confirm-count {counts['pending']}")
    return "\n".join(lines)


def apply(api, plan, ops, out):
    """Run pending writes in order. Milestone operations come first, so a title
    created by this run is looked up after it exists."""
    titles = None
    for op in ops:
        if op["status"] != "pending":
            continue
        payload = op["payload"]
        if payload and isinstance(payload.get("milestone"), str):
            if titles is None:
                titles = {m["title"]: m["number"]
                          for m in api.get_all(f"repos/{plan['repository']}/milestones?state=all")}
            _require(payload["milestone"] in titles, f"milestone \"{payload['milestone']}\" not found")
            payload = {**payload, "milestone": titles[payload["milestone"]]}
        try:
            api.write(op["method"], op["path"], payload)
            if op["path"].endswith("/milestones"):
                titles = None  # look the titles up again after creating one
        except PlanError as error:
            raise PlanError(f"stopped at {op['id']}: {error}. Rerun the dry run; completed writes are "
                            "detected and skipped.") from None
        print(f"applied {op['id']}: {op['label']}", file=out)


def _display(path):
    """Show the plan path relative to the repository, never a home directory."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).name


def main(argv=None, runner=gh, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", default=str(PLAN), help="plan file (default: scripts/tracker/tracker-plan.json)")
    parser.add_argument("--no-fetch", action="store_true",
                        help="do not read GitHub; validate the plan and list every operation")
    parser.add_argument("--apply", action="store_true", help="perform the pending writes (needs --confirm-count)")
    parser.add_argument("--confirm-count", type=int, metavar="N",
                        help="the number of pending operations printed by the reviewed dry run")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.plan)
        if args.apply and args.no_fetch:
            raise PlanError("--apply needs the current state; it cannot be combined with --no-fetch")
        if args.apply and args.confirm_count is None:
            raise PlanError("--apply refused: pass --confirm-count N with the pending count from a reviewed dry run")
        api = GitHub(plan["repository"], runner)
        state = None if args.no_fetch else fetch_state(api, plan)
        ops = operations(plan, state)
        pending = sum(op["status"] == "pending" for op in ops)
        confirmed = args.apply and args.confirm_count == pending
        mode = "offline" if state is None else "apply" if confirmed else "dry-run"
        print(render(plan, ops, mode=mode, source=_display(args.plan)), file=out)
        if args.apply and not confirmed:
            raise PlanError(f"--apply refused: {pending} operations are pending, "
                            f"but --confirm-count is {args.confirm_count}; rerun the dry run and review it")
        if confirmed:
            apply(api, plan, ops, out)
    except PlanError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
