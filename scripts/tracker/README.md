# Tracker and branch clean-up (dry-run first)

Both tools read GitHub through an authenticated `gh` CLI. They change nothing
unless a maintainer explicitly asks, after reviewing the dry run.

## Issue tracker plan

`tracker-plan.json` is reviewed data. It holds, per issue: roadmap item ids,
label changes, a short factual comment, milestone moves and proposed closures.
It also lists new issues for untracked defects and deliverables, and the
decisions only the team can make. It was built from read-only queries on
12 Nov 2026 and the evaluation report's defect list.

```text
python scripts/tracker/plan.py              # dry run: reads GitHub, prints every pending write
python scripts/tracker/plan.py --no-fetch   # offline: validates the plan and lists all operations
```

The dry run prints each write with its method, API path and full payload.
Comments and new issues carry a hidden `<!-- tracker-plan:ID -->` marker, and
labels, milestones and closures are compared with the current state, so a
rerun skips work already done.

Closures, and actions marked `needs_sign_off`, stay **blocked** until the plan
names who signed them off in `signed_off_by`. Nothing in the committed plan is
signed off.

`--apply` refuses unless `--confirm-count N` equals the number of pending
operations in that run. Announce the batch to the team before applying it;
it notifies everyone watching the issues.

## Branches

```text
python scripts/tracker/branches.py          # dry run only
python scripts/tracker/branches.py --json
```

Branches with no commits outside `main` are listed for deletion, each after an
`archive/<name>` tag. Unmerged branches idle for more than 14 days get a tag
only; their owners decide. The default branch, protected branches and any
branch used by an open pull request are kept. The tool never writes; a
maintainer runs the printed commands after review. `--apply` is refused.

Each branch gets one command line joined with `&&`, so a failed tag or tag
push stops the delete. The delete uses `--force-with-lease` on the SHA the
survey saw. If someone pushed to the branch after the survey, git refuses the
delete and the branch stays; re-run the survey before trying again. Commands
use POSIX shell quoting (Git Bash, not Windows PowerShell 5.1).

Tests use a fake `gh` runner and make no network calls:

```text
python -m unittest tests.test_tracker_plan tests.test_tracker_branches -v
```
