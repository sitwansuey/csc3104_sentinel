"""Update the Sentinel Project from branch/PR metadata; never run feature code."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPOSITORY = "sitwansuey/csc3104_sentinel"
PROJECT = "PVT_kwDOE8Kj6M4Boi1V"
STATUS_FIELD = "PVTSSF_lADOE8Kj6M4Boi1VzhnCctU"
START_FIELD = "PVTF_lADOE8Kj6M4Boi1VzhnFVyc"
AUTOMATION_FIELD = "PVTF_lADOE8Kj6M4Boi1VzhnOgw0"
QUEUED = {"Todo": "f75ad846", "Backlog": "642d25f0"}
IN_PROGRESS = "47fc9ee4"
READY = "d648e700"
BRANCH = re.compile(r"^(?:feat|feature|fix|bugfix|chore|docs|test|refactor)/(\d+)-[a-zA-Z0-9][a-zA-Z0-9._/-]*$")


def branch_issue(branch):
    match = BRANCH.fullmatch(branch or "")
    return int(match[1]) if match and int(match[1]) > 0 else None


def first_work_date(comparison):
    """Only commits unique to this branch count; exclude branch/merge-only pushes."""
    commits = comparison.get("commits", [])
    # GitHub's comparison response can truncate after 250 commits. Do not guess.
    if comparison.get("total_commits", 0) > len(commits):
        raise ValueError("Branch comparison is truncated; set the historical Start date manually")
    for commit in commits:
        if len(commit.get("parents", [])) == 1:
            value = commit["commit"]["committer"]["date"]
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.utcoffset() is None:
                raise ValueError("Commit date must include a timezone")
            return stamp.astimezone(timezone(timedelta(hours=8))).date().isoformat()
    return None


def desired_status(current):
    return IN_PROGRESS if current == "Todo" else None


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, path, payload=None):
        request = Request("https://api.github.com/" + path,
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise RuntimeError(f"GitHub returned HTTP {code}; check PROJECT_TOKEN permissions") from None

    def graphql(self, query, **variables):
        result = self.request("graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            # Never print request headers or credential-bearing debug output.
            raise RuntimeError("GitHub GraphQL rejected the request; check project access and schema")
        return result["data"]

    def issue(self, number):
        return self.graphql('''query($number:Int!) {
          repository(owner:"sitwansuey", name:"csc3104_sentinel") {
            issue(number:$number) { id number state }
          }
        }''', number=number)["repository"]["issue"]

    def items(self):
        items, cursor = {}, None
        while True:
            result = self.graphql('''query($project:ID!, $cursor:String) {
              node(id:$project) { ... on ProjectV2 {
                items(first:100,after:$cursor) {
                  nodes { id content { ... on Issue { id } ... on PullRequest { id } }
                    status:fieldValueByName(name:"Status") {
                      ... on ProjectV2ItemFieldSingleSelectValue { name }
                    }
                    start:fieldValueByName(name:"Start date") {
                      ... on ProjectV2ItemFieldDateValue { date }
                    }
                    automation:fieldValueByName(name:"Automation state") {
                      ... on ProjectV2ItemFieldTextValue { text }
                    }
                  }
                  pageInfo { hasNextPage endCursor }
                }
              } }
            }''', project=PROJECT, cursor=cursor)["node"]["items"]
            for item in result["nodes"]:
                if item["content"]:
                    items[item["content"]["id"]] = item
            if not result["pageInfo"]["hasNextPage"]:
                return items
            cursor = result["pageInfo"]["endCursor"]

    def update(self, item_id, field, value):
        self.graphql('''mutation($project:ID!, $item:ID!, $field:ID!, $value:ProjectV2FieldValue!) {
          updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
            fieldId:$field,value:$value}) { projectV2Item { id } }
        }''', project=PROJECT, item=item_id, field=field, value=value)


def update_item(api, item, *, date=None):
    current = (item.get("status") or {}).get("name")
    next_status = desired_status(current)
    if next_status:
        api.update(item["id"], STATUS_FIELD, {"singleSelectOptionId": next_status})
    # Preserve historical dates and leave intentionally queued/done work alone.
    if date and not item.get("start") and current in {"Todo", "In Progress", "Ready To Merge"}:
        api.update(item["id"], START_FIELD, {"date": date})


def automation_state(item):
    text = (item.get("automation") or {}).get("text")
    if not text:
        return {}
    state = json.loads(text)
    if (not isinstance(state, dict) or state.get("queued") not in QUEUED
            or type(state.get("label_controlled")) is not bool
            or (state["label_controlled"] and type(state.get("pr")) is not int)):
        raise ValueError("Invalid Automation state; review the card before retrying")
    return state


def save_state(api, item, state):
    if state != automation_state(item):
        text = json.dumps(state, sort_keys=True)
        api.update(item["id"], AUTOMATION_FIELD, {"text": text})
        item["automation"] = {"text": text}


def remember_queue(api, item):
    """Capture queue state before a push moves Todo to In Progress."""
    current = (item.get("status") or {}).get("name")
    state = automation_state(item)
    if current in QUEUED and not state.get("label_controlled"):
        save_state(api, item, {"queued": current, "label_controlled": False})


def update_label_item(api, item, pr_number, labels):
    current = (item.get("status") or {}).get("name")
    if current not in {*QUEUED, "In Progress", "Ready To Merge"}:
        return
    state = automation_state(item)
    if state.get("label_controlled") and state["pr"] != pr_number:
        print("Card is controlled by another PR; coordinate its labels before changing status")
        return
    # Explicit work-in-progress wins if the user accidentally leaves both labels.
    target = IN_PROGRESS if "in progress" in labels else READY if "ready to merge" in labels else None
    if target:
        queued = current if current in QUEUED else state.get("queued", "Todo")
        save_state(api, item, {"queued": queued, "label_controlled": True, "pr": pr_number})
    elif state.get("label_controlled"):
        target = QUEUED[state["queued"]]
        current_id = {**QUEUED, "In Progress": IN_PROGRESS, "Ready To Merge": READY}[current]
        if target != current_id:
            api.update(item["id"], STATUS_FIELD, {"singleSelectOptionId": target})
        # Clear control after restoration succeeds so a failed write can be retried.
        save_state(api, item, {"queued": state["queued"], "label_controlled": False})
        return
    else:
        # An unrelated/unlabelled PR never resets work started by a branch push.
        return
    current_id = {**QUEUED, "In Progress": IN_PROGRESS, "Ready To Merge": READY}[current]
    if target != current_id:
        api.update(item["id"], STATUS_FIELD, {"singleSelectOptionId": target})


def handle_push(api, event):
    run = event.get("workflow_run", {})
    if run.get("event") != "push" or run.get("repository", {}).get("full_name") != REPOSITORY:
        return
    branch = run.get("head_branch")
    number = branch_issue(branch)
    if number is None:
        print("No issue-number branch prefix; no project changes")
        return
    issue = api.issue(number)
    if not issue or issue["state"] != "OPEN":
        return
    # Compare the event's exact commit with main. Creating a branch at main is no work.
    base = event["repository"]["default_branch"]
    comparison = api.request(f"repos/{REPOSITORY}/compare/{base}...{run['head_sha']}")
    date = first_work_date(comparison)
    if date is None:
        return
    item = api.items().get(issue["id"])
    if not item:
        print(f"Issue #{number} is not in the project; add it before tracking work")
        return
    remember_queue(api, item)
    update_item(api, item, date=date)
    print(f"Processed pushed work for issue #{number}; existing dates and advanced statuses preserved")
    # A newer push event may replace a queued label event in the concurrency group.
    # Reconcile the current PR label as well, rather than depending on event order.
    query = urlencode({"state": "open", "head": "sitwansuey:" + branch, "per_page": 100})
    for pull in api.request(f"repos/{REPOSITORY}/pulls?{query}"):
        handle_pr(api, {"pull_request": {"number": pull["number"]}})


def handle_pr(api, event):
    number = event["pull_request"]["number"]
    # Read current metadata so a delayed label event cannot mark a closed/draft PR ready.
    pull = api.graphql('''query($number:Int!) {
      repository(owner:"sitwansuey",name:"csc3104_sentinel") {
        pullRequest(number:$number) { id state isDraft headRefName
          labels(first:100) { nodes { name } }
          closingIssuesReferences(first:100) {
            nodes { id number state repository { nameWithOwner } }
            pageInfo { hasNextPage }
          }
        }
      }
    }''', number=number)["repository"]["pullRequest"]
    if not pull or pull["state"] != "OPEN":
        return
    labels = {label["name"] for label in pull["labels"]["nodes"]}
    if pull["isDraft"]:
        labels.discard("ready to merge")
    if pull["closingIssuesReferences"]["pageInfo"]["hasNextPage"]:
        raise ValueError("PR has more than 100 closing issues; review its scope")
    issues = {issue["id"]: issue for issue in pull["closingIssuesReferences"]["nodes"]
              if issue["repository"]["nameWithOwner"] == REPOSITORY and issue["state"] == "OPEN"}
    number = branch_issue(pull["headRefName"])
    if number:
        issue = api.issue(number)
        if issue and issue["state"] == "OPEN":
            issues[issue["id"]] = issue
    items = api.items()
    for node_id in [pull["id"], *issues]:
        if node_id in items:
            update_label_item(api, items[node_id], pr_number=event["pull_request"]["number"], labels=labels)
    print("Reconciled current PR labels for existing project cards; no new PR cards created")


def main():
    token = os.environ.get("PROJECT_TOKEN")
    if not token:
        raise RuntimeError("Add the PROJECT_TOKEN Actions secret; see docs/project-automation.md")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if event.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("This automation is scoped to the Sentinel repository")
    api = GitHub(token)
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
        handle_push(api, event)
    elif os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target":
        handle_pr(api, event)


if __name__ == "__main__":
    main()
