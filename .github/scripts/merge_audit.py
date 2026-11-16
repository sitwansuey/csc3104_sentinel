"""Report main-branch merges that lack a non-author approval or green checks.

Report-only: branch protection cannot be enforced on this private free-plan
repository, so this makes unreviewed merges visible instead of blocking them.
Every API failure is reported as UNVERIFIED, never as reviewed.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = "sitwansuey/csc3104_sentinel"
PAGE = 100
MAX_PAGES = 10
# GitHub push payloads list at most 20 commits; more may have been pushed.
PUSH_COMMIT_LIMIT = 20
GREEN = {"success", "neutral", "skipped"}


class AuditError(RuntimeError):
    """A response that cannot be trusted; the merge is reported as unverified."""


class GitHub:
    """Read-only REST client for the workflow token."""

    def __init__(self, token):
        self.token = token

    def get(self, path):
        request = Request("https://api.github.com/" + path,
                          headers={"Authorization": "Bearer " + self.token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise AuditError(f"GitHub returned HTTP {code} for {path.split('?')[0]}") from None
        except URLError as exc:
            raise AuditError(f"GitHub request failed: {exc.reason}") from None


class GhCli:
    """Read-only client through an authenticated gh CLI, for local audits."""

    def get(self, path):
        result = subprocess.run(["gh", "api", "--method", "GET", path],
                                capture_output=True, text=True, encoding="utf-8", check=False, timeout=60)
        if result.returncode:
            raise AuditError(f"gh api failed for {path.split('?')[0]}: {result.stderr.strip()[:200]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            raise AuditError(f"Unreadable response for {path.split('?')[0]}") from None


def get_all(api, path):
    """Fetch every page of a list endpoint; refuse to guess past the page cap."""
    items = []
    joiner = "&" if "?" in path else "?"
    for page in range(1, MAX_PAGES + 1):
        batch = api.get(f"{path}{joiner}per_page={PAGE}&page={page}")
        if not isinstance(batch, list):
            raise AuditError(f"Unexpected response for {path.split('?')[0]}")
        items.extend(batch)
        if len(batch) < PAGE:
            return items
    raise AuditError(f"More than {PAGE * MAX_PAGES} items for {path.split('?')[0]}; response truncated")


def approval(reviews, author, head_sha):
    """Split non-author logins whose latest decisive review is APPROVED by commit.

    Returns (current, stale): approvals given on the merged head commit, and
    approvals given on an earlier commit (or with no commit recorded), which do
    not cover the changes pushed after them.
    """
    latest = {}
    for review in reviews:
        login = (review.get("user") or {}).get("login")
        state = review.get("state")
        # A comment does not change a reviewer's verdict; a dismissal cancels it.
        if not login or state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest[login] = (state, review.get("commit_id"))
    approved = {login: commit for login, (state, commit) in latest.items()
                if state == "APPROVED" and login.lower() != (author or "").lower()}
    current = sorted(login for login, commit in approved.items() if head_sha and commit == head_sha)
    return current, sorted(set(approved) - set(current))


def checks_state(runs):
    if not runs:
        return "none found"
    pending = [run["name"] for run in runs if run.get("status") != "completed"]
    if pending:
        return "pending: " + ", ".join(sorted(pending))
    failed = [run["name"] for run in runs if run.get("conclusion") not in GREEN]
    return "failed: " + ", ".join(sorted(failed)) if failed else "green"


def audit_pull(api, number, base):
    pull = api.get(f"repos/{REPOSITORY}/pulls/{number}")
    if not isinstance(pull, dict):
        raise AuditError(f"Unexpected response for PR #{number}")
    author = (pull.get("user") or {}).get("login")
    head_sha = pull["head"]["sha"]
    approvers, stale = approval(get_all(api, f"repos/{REPOSITORY}/pulls/{number}/reviews"), author, head_sha)
    data = api.get(f"repos/{REPOSITORY}/commits/{head_sha}/check-runs?per_page={PAGE}")
    if not isinstance(data, dict) or not isinstance(data.get("check_runs", []), list):
        raise AuditError(f"Unexpected check runs for PR #{number}")
    runs = data.get("check_runs", [])
    if data.get("total_count", 0) > len(runs):
        raise AuditError(f"Check runs for PR #{number} are truncated")
    checks = checks_state(runs)
    problems = []
    if not approvers and stale:
        problems.append("approved only on an earlier commit (" + ", ".join(stale) +
                        "); later changes were not reviewed")
    elif not approvers:
        problems.append("no approving review from someone other than the author")
    if checks != "green":
        problems.append("checks " + checks)
    return {"subject": f"PR #{number}", "title": pull.get("title", ""),
            "approvers": approvers, "checks": checks, "problems": problems,
            "merged_into": base}


def audit_commit(api, sha, branch):
    """Group a pushed commit under the merged PR that brought it to the branch."""
    pulls = get_all(api, f"repos/{REPOSITORY}/commits/{sha}/pulls")
    if not all(isinstance(pull, dict) for pull in pulls):
        raise AuditError(f"Unexpected pull list for {sha[:7]}")
    merged = [pull["number"] for pull in pulls
              if pull.get("merged_at") and pull.get("base", {}).get("ref") == branch]
    return sorted(merged)


def audit(api, shas, branch="main"):
    """Return one row per merged PR or direct push; API errors become UNVERIFIED rows."""
    rows, seen = [], set()
    for sha in shas:
        try:
            numbers = audit_commit(api, sha, branch)
        except (AuditError, KeyError, TypeError) as error:
            rows.append({"subject": sha[:7], "title": "", "approvers": [], "checks": "unknown",
                         "problems": ["UNVERIFIED: " + str(error)]})
            continue
        if not numbers:
            rows.append({"subject": sha[:7], "title": "direct push", "approvers": [], "checks": "unknown",
                         "problems": ["pushed without a merged pull request"]})
            continue
        for number in numbers:
            if number in seen:
                continue
            seen.add(number)
            try:
                rows.append(audit_pull(api, number, branch))
            except (AuditError, KeyError, TypeError) as error:
                rows.append({"subject": f"PR #{number}", "title": "", "approvers": [], "checks": "unknown",
                             "problems": ["UNVERIFIED: " + str(error)]})
    return rows


def render(rows, note=None):
    flagged = [row for row in rows if row["problems"]]
    lines = ["## Merge audit (report-only)", "",
             f"{len(flagged)} of {len(rows)} audited merges need attention. "
             "Branch protection is not available on this plan; this report does not block merges.", ""]
    if note:
        lines += [note, ""]
    lines += ["| Merge | Title | Non-author approvals | Checks on PR head | Finding |",
              "| --- | --- | --- | --- | --- |"]
    for row in rows:
        title = row["title"].replace("|", "\\|")[:80]
        lines.append(f"| {row['subject']} | {title} | {', '.join(row['approvers']) or 'none'} | "
                     f"{row['checks']} | {'; '.join(row['problems']) or 'ok'} |")
    return "\n".join(lines) + "\n"


def push_shas(event):
    commits = event.get("commits") or []
    shas = [commit["id"] for commit in commits if commit.get("id")]
    if event.get("after") and event["after"] not in shas:
        shas.append(event["after"])
    note = None
    if len(commits) >= PUSH_COMMIT_LIMIT:
        note = (f"The push listed {len(commits)} commits, the payload limit; "
                "earlier commits may not be audited. Run with --recent to check them.")
    return shas, note


def main(argv=None, api=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sha", action="append", default=[], help="audit this commit on main (repeatable)")
    parser.add_argument("--recent", type=int, metavar="N",
                        help=f"audit the last N commits on main (1 to {PAGE})")
    parser.add_argument("--via-gh", action="store_true", help="read through the local gh CLI instead of a token")
    parser.add_argument("--strict", action="store_true", help="exit 1 when any merge is flagged")
    args = parser.parse_args(argv)
    if args.recent is not None and not 1 <= args.recent <= PAGE:
        parser.error(f"--recent must be between 1 and {PAGE}; use --sha for older commits")
    if api is None:
        if args.via_gh:
            api = GhCli()
        else:
            token = environ.get("GITHUB_TOKEN")
            if not token:
                raise SystemExit("Set GITHUB_TOKEN (the workflow token) or use --via-gh")
            api = GitHub(token)
    note = None
    if args.sha:
        shas = args.sha
    elif args.recent:
        commits = api.get(f"repos/{REPOSITORY}/commits?sha=main&per_page={args.recent}")
        if not isinstance(commits, list) or not all(isinstance(c, dict) and c.get("sha") for c in commits):
            raise SystemExit("Unexpected commit list for main; nothing audited")
        shas = [commit["sha"] for commit in commits]
    else:
        event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
        if event.get("repository", {}).get("full_name") != REPOSITORY:
            raise SystemExit("This audit is scoped to the Sentinel repository")
        shas, note = push_shas(event)
    rows = audit(api, shas)
    report = render(rows, note)
    print(report)
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(report)
    for row in rows:
        if row["problems"]:
            # Annotations show on the run page; the job itself stays green (report-only).
            print(f"::warning title=Merge audit::{row['subject']}: {'; '.join(row['problems'])}")
    flagged = sum(bool(row["problems"]) for row in rows)
    return 1 if args.strict and flagged else 0


if __name__ == "__main__":
    sys.exit(main())
