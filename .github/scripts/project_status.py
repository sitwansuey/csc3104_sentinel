"""Start explicitly linked issues once on pushed work; ignore labels and PR status."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPOSITORY = "sitwansuey/csc3104_sentinel"
PROJECT = "PVT_kwDOE8Kj6M4Boi1V"
STATUS_FIELD = "PVTSSF_lADOE8Kj6M4Boi1VzhnCctU"
START_FIELD = "PVTF_lADOE8Kj6M4Boi1VzhnFVyc"
AUTOMATION_FIELD = "PVTF_lADOE8Kj6M4Boi1VzhnOgw0"
IN_PROGRESS = "47fc9ee4"


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


class GitHub:
    def __init__(self, token, repository_token=None, *, token_name="PROJECT_TOKEN"):
        self.token = token
        self.token_name = token_name
        # Organization Projects credentials need not grant repository Contents
        # access. Use the workflow's scoped token for private repository reads.
        self.repository_api = (GitHub(repository_token, token_name="GITHUB_TOKEN")
                               if repository_token else self)

    def request(self, path, payload=None):
        if path.startswith("repos/") and self.repository_api is not self:
            return self.repository_api.request(path, payload)
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
            raise RuntimeError(f"GitHub returned HTTP {code} using {self.token_name}; "
                               "check resource existence and token access") from None

    def graphql(self, query, **variables):
        result = self.request("graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            # Never print request headers or credential-bearing debug output.
            raise RuntimeError(f"GitHub GraphQL rejected the request using {self.token_name}; "
                               "check resource access and schema")
        return result["data"]

    def items(self):
        items, cursor = {}, None
        while True:
            result = self.graphql('''query($project:ID!, $cursor:String) {
              node(id:$project) { ... on ProjectV2 {
                items(first:100,after:$cursor) {
                  nodes { id content { ... on Issue { id number state repository { nameWithOwner }
                      linkedBranches(first:100) {
                        nodes { ref { name repository { nameWithOwner } } }
                        pageInfo { hasNextPage }
                      }
                    } ... on PullRequest { id } }
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
                if item["content"] and item["content"].get("repository", {}).get("nameWithOwner") == REPOSITORY:
                    items[item["content"]["id"]] = item
            if not result["pageInfo"]["hasNextPage"]:
                return items
            cursor = result["pageInfo"]["endCursor"]

    def pull_issues(self, branch):
        # Opening a PR replaces the issue's Development branch link with a PR link.
        query = urlencode({"state": "open", "head": "sitwansuey:" + branch, "per_page": 100})
        pulls = self.request(f"repos/{REPOSITORY}/pulls?{query}")
        if len(pulls) == 100:
            raise ValueError("Too many open PRs for one branch")
        result = set()
        for pull in pulls:
            linked = self.repository_api.graphql("""query($number:Int!) {
              repository(owner:"sitwansuey",name:"csc3104_sentinel") {
                pullRequest(number:$number) { closingIssuesReferences(first:100) {
                  nodes { id repository { nameWithOwner } }
                  pageInfo { hasNextPage }
                } }
              }
            }""", number=pull["number"])["repository"]["pullRequest"]["closingIssuesReferences"]
            if linked["pageInfo"]["hasNextPage"]:
                raise ValueError("PR issue links are truncated")
            result.update(issue["id"] for issue in linked["nodes"]
                          if issue["repository"]["nameWithOwner"] == REPOSITORY)
        return result

    def update(self, item_id, field, value):
        self.graphql('''mutation($project:ID!, $item:ID!, $field:ID!, $value:ProjectV2FieldValue!) {
          updateProjectV2ItemFieldValue(input:{projectId:$project,itemId:$item,
            fieldId:$field,value:$value}) { projectV2Item { id } }
        }''', project=PROJECT, item=item_id, field=field, value=value)


def linked_to_branch(item, branch):
    links = item["content"]["linkedBranches"]
    if links["pageInfo"]["hasNextPage"]:
        raise ValueError("Issue branch links are truncated")
    return any(link.get("ref") and link["ref"]["name"] == branch
               and link["ref"]["repository"]["nameWithOwner"] == REPOSITORY
               for link in links["nodes"])


def start_once(api, item, date):
    content = item["content"]
    if content["state"] != "OPEN":
        return
    text = (item.get("automation") or {}).get("text")
    saved = json.loads(text) if text else {}
    if not isinstance(saved, dict):
        raise ValueError("Invalid Automation state")
    if saved.get("work_started") is True:
        return
    current = (item.get("status") or {}).get("name")
    if current in {"Todo", "Backlog"}:
        api.update(item["id"], STATUS_FIELD, {"singleSelectOptionId": IN_PROGRESS})
    if current in {"Todo", "Backlog", "In Progress", "Ready To Merge"} and not item.get("start"):
        api.update(item["id"], START_FIELD, {"date": date})
    # Write last so another run can finish an interrupted start.
    api.update(item["id"], AUTOMATION_FIELD, {"text": '{"work_started":true}'})
    print(f"Recorded first work for issue #{content['number']}; later statuses are manual")


def handle_push(api, event):
    run = event.get("workflow_run", {})
    if (run.get("event") != "push" or run.get("repository", {}).get("full_name") != REPOSITORY
            or not run.get("head_branch")
            or run["head_branch"] == event["repository"]["default_branch"]):
        return
    comparison = api.request(f"repos/{REPOSITORY}/compare/{event['repository']['default_branch']}...{run['head_sha']}")
    date = first_work_date(comparison)
    if date is None:
        return
    items = api.items()
    branch = run["head_branch"]
    ids = {node_id for node_id, item in items.items() if linked_to_branch(item, branch)}
    ids.update(api.pull_issues(branch))
    for node_id in ids & items.keys():
        start_once(api, items[node_id], date)
    if not ids & items.keys():
        print("No linked project issue; link the branch through the issue's Development section")


def main():
    if os.environ.get("GITHUB_EVENT_NAME") != "workflow_run":
        return
    token = os.environ.get("PROJECT_TOKEN")
    if not token:
        raise RuntimeError("Add the PROJECT_TOKEN Actions secret; see docs/project-automation.md")
    repository_token = os.environ.get("GITHUB_TOKEN")
    if not repository_token:
        raise RuntimeError("Pass the workflow GITHUB_TOKEN for repository reads; see docs/project-automation.md")
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    if event.get("repository", {}).get("full_name") != REPOSITORY:
        raise ValueError("This automation is scoped to the Sentinel repository")
    api = GitHub(token, repository_token)
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_run":
        handle_push(api, event)


if __name__ == "__main__":
    main()
