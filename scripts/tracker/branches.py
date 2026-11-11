"""List stale remote branches with an archive-tag plan. Dry-run only.

Reads the repository's branches with read-only `gh api` GETs and sorts them:

- merged: no commits that the default branch lacks. Plan: archive tag, then delete.
- inactive: unmerged, last commit older than --inactive-days. Plan: archive tag;
  the branch owner decides whether to merge, keep or delete it.
- kept: the default branch, protected branches, branches used by an open pull
  request (as head or base) and recently active unmerged branches.

Nothing is written. The printed git commands are for a maintainer to run after
review. Each branch gets one command line joined with '&&': the archive tag is
created and pushed first, and the delete carries a lease on the surveyed SHA.
If the branch has moved since the survey, or the tag cannot be created or
pushed, git refuses and the branch stays. Re-run the survey in that case.
"""

import argparse
from datetime import datetime, timedelta, UTC
import json
import shlex
import subprocess
import sys
from urllib.parse import quote

REPOSITORY = "sitwansuey/csc3104_sentinel"
PAGE = 100
MAX_PAGES = 10


class BranchError(RuntimeError):
    """A response that cannot be trusted; no plan is printed."""


def gh(args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, encoding="utf-8", check=False,
                            timeout=60)
    if result.returncode:
        raise BranchError(f"gh {' '.join(args[:3])} failed: {result.stderr.strip()[:300]}")
    return result.stdout


class GitHub:
    def __init__(self, repository, runner=gh):
        self.repository = repository
        self.runner = runner

    def get(self, path):
        text = self.runner(["api", "--method", "GET", f"repos/{self.repository}{path}"])
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise BranchError(f"unreadable response for {path}") from None

    def get_all(self, path):
        items = []
        joiner = "&" if "?" in path else "?"
        for page in range(1, MAX_PAGES + 1):
            batch = self.get(f"{path}{joiner}per_page={PAGE}&page={page}")
            if not isinstance(batch, list):
                raise BranchError(f"unexpected response for {path}")
            items.extend(batch)
            if len(batch) < PAGE:
                return items
        raise BranchError(f"more than {PAGE * MAX_PAGES} items for {path}; refusing to guess")


def survey(api, now, inactive_days):
    """Classify every branch; any failed or odd response stops the survey."""
    default = api.get("")["default_branch"]
    pulls = api.get_all("/pulls?state=open")
    in_use = {}
    for pull in pulls:
        for side in ("head", "base"):
            ref = pull[side]
            if (ref.get("repo") or {}).get("full_name") == api.repository:
                in_use.setdefault(ref["ref"], []).append(f"#{pull['number']} {side}")
    rows = []
    for branch in api.get_all("/branches"):
        name, sha = branch["name"], branch["commit"]["sha"]
        row = {"name": name, "sha": sha, "ahead": None, "behind": None, "last_commit": None}
        if name == default or branch.get("protected"):
            rows.append({**row, "kind": "kept", "reason": "default branch" if name == default else "protected"})
            continue
        # Totals do not depend on paging; one commit per page keeps the response small.
        compare = api.get(f"/compare/{quote(default, safe='')}...{quote(sha, safe='')}?per_page=1")
        if compare.get("status") not in {"identical", "behind", "ahead", "diverged"}:
            raise BranchError(f"unexpected comparison for {name}")
        detail = api.get(f"/branches/{quote(name, safe='')}")
        if detail["commit"]["sha"] != sha:
            raise BranchError(f"{name} moved during the survey; run it again")
        date = detail["commit"]["commit"]["committer"]["date"]
        row.update(ahead=compare["ahead_by"], behind=compare["behind_by"], last_commit=date[:10])
        if name in in_use:
            rows.append({**row, "kind": "kept", "reason": "open pull request " + ", ".join(in_use[name])})
        elif compare["ahead_by"] == 0:
            rows.append({**row, "kind": "merged", "reason": f"fully contained in {default}"})
        else:
            stamp = datetime.fromisoformat(date.replace("Z", "+00:00"))
            if now - stamp > timedelta(days=inactive_days):
                rows.append({**row, "kind": "inactive",
                             "reason": f"unmerged commits: {compare['ahead_by']}, last on {date[:10]}"})
            else:
                rows.append({**row, "kind": "kept", "reason": "recently active"})
    return default, rows


def archive_tag(name):
    return "archive/" + name


def commands(row):
    """Shell commands for one branch; the tag is always created and pushed first.

    The delete is leased on the surveyed SHA, so git rejects it if anyone has
    pushed to the branch since the survey; those commits would be in no tag.
    """
    tag = archive_tag(row["name"])
    ref = "refs/heads/" + row["name"]
    message = f"Archive of branch {row['name']} at {row['sha'][:7]}"
    steps = [f"git tag -a {shlex.quote(tag)} {row['sha']} -m {shlex.quote(message)}",
             f"git push origin {shlex.quote('refs/tags/' + tag)}"]
    if row["kind"] == "merged":
        lease = f"--force-with-lease={ref}:{row['sha']}"
        steps.append(f"git push {shlex.quote(lease)} origin --delete {shlex.quote(ref)}")
    return steps


def command_line(row):
    """All steps for one branch on one line; a failed step stops the rest."""
    return " && ".join(commands(row))


def render(default, rows, inactive_days):
    lines = [f"Branch survey for {REPOSITORY} (default branch {default}). DRY RUN: nothing is written.",
             "Run 'git fetch origin' before the commands below so every commit exists locally.", ""]
    for kind, title in (("merged", "Merged: archive tag, then delete"),
                        ("inactive", f"Inactive over {inactive_days} days and unmerged: archive tag; "
                                     "the owner decides"),
                        ("kept", "Kept")):
        group = [row for row in rows if row["kind"] == kind]
        lines.append(f"{title} ({len(group)})")
        for row in sorted(group, key=lambda r: r["name"]):
            detail = f"ahead {row['ahead']}, behind {row['behind']}, last commit {row['last_commit']}; " \
                if row["ahead"] is not None else ""
            lines.append(f"  {row['name']} {row['sha'][:7]}: {detail}{row['reason']}")
            if kind != "kept":
                lines.append("      " + command_line(row))
        lines.append("")
    lines.append("Review this list with the branch owners. Run each branch's line whole: a failed step "
                 "stops the rest. A delete is refused if the branch moved after this survey; re-run the "
                 "survey then. Branch deletion has no effect on the Project board. Restore a branch with: "
                 "git push origin refs/tags/archive/<name>:refs/heads/<name>")
    return "\n".join(lines)


def main(argv=None, runner=gh, now=None, out=None):
    out = out or sys.stdout
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inactive-days", type=int, default=14,
                        help="unmerged branches idle longer than this are listed for triage (default 14)")
    parser.add_argument("--json", action="store_true", help="print the survey as JSON")
    parser.add_argument("--apply", action="store_true",
                        help="refused: this tool never writes; run the reviewed commands by hand")
    args = parser.parse_args(argv)
    if args.apply:
        print("error: --apply refused: branches.py is dry-run only. After review, a maintainer runs "
              "the printed commands; each delete follows its archive tag and is leased on the "
              "surveyed SHA.", file=sys.stderr)
        return 2
    if args.inactive_days < 1:
        parser.error("--inactive-days must be at least 1")
    try:
        default, rows = survey(GitHub(REPOSITORY, runner), now or datetime.now(UTC), args.inactive_days)
    except (BranchError, KeyError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"default": default, "branches": [{**row, "commands": commands(row),
                                                           "command_line": command_line(row)}
                                                          if row["kind"] != "kept" else row for row in rows]},
                         indent=2), file=out)
    else:
        print(render(default, rows, args.inactive_days), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
