# Project automation: first work only

1. Put an issue in Sentinel Project 2 as **Todo** or **Backlog**.
2. On GitHub use issue **Development → Create a branch**, with any branch name.
   Existing branches can also be linked through Development before pushing work.
3. Check out the branch, commit normally and push. No commit-message or branch-name
   format is required.
4. The first observed push with branch-specific non-merge work moves its explicitly
   linked open issue to **In Progress**. Local commits are invisible until pushed;
   creating a branch at main alone does not start work.
5. Change later statuses directly in the Project. **Labels no longer move cards.**
   After first work has been recorded, later pushes do not override manual status
   changes. Branch deletion has no status effect.

GitHub replaces a Development branch link with its linked PR when a PR opens.
The script follows that open PR's closing-issue links too. It never guesses issue
numbers from branch names or commit messages. Only same-repository issues already
in the Project are considered; no labels, issue cards or PR cards are created.

An empty Start date is filled from the first branch-specific non-merge commit's
committer date in Singapore time. Existing dates are retained. Start from main
rather than unrelated feature work. Truncated history/link responses fail rather
than guessing. Work pushed before linking needs another push or a rerun of its
**Project status** run after linking. Rerunning Checks itself does not generate a
new requested event.

The existing **Automation state** field stores `{"work_started":true}`, preventing
later pushes from undoing deliberate status changes. Obsolete queue/PR-label
bookkeeping is replaced when work is first observed under the new workflow.
Do not edit this field manually; omit it from normal views. First observation
after installation can pick up existing unmerged work, but not already-merged
history. The marker is per issue, even if the issue later uses another branch.

## Activation and credentials

Keep the existing **PROJECT_TOKEN** secret with organization Projects read/write
and visibility of this repository's issues in that Project. The normal repository
**GITHUB_TOKEN** reads repository commits, PRs and their closing-issue links;
the workflow supplies it automatically with read-only Contents, Issues and
Pull requests permissions. No second repository secret is required. The normal
repository token cannot edit organization Projects. Never place tokens in source,
issues or chat.

The earlier implementation reused PROJECT_TOKEN for commit comparisons. The
observed repeated HTTP 404 failures occurred there while Checks passed and the
same comparison succeeded with repository access. Missing private-repository
Contents access is the likely cause; this token's settings are not inspectable
from the Actions secret. Repository
reads and Project operations now use their respective tokens. A 404 can also mean
a missing commit/resource; errors identify the token role without printing it.
Workflow changes activate only after review and merge to the default branch.

Merge the follow-up PR to replace PR #31's already-installed label automation.
Until then, its old label rules remain active. Afterwards test a linked branch's
first work push, a manual return to Todo and a later push. Unit tests use fake
APIs and do not prove real event-driven board updates.

Completion, Not planned closure and Will Not Implement are outside this first-work
workflow. Configure them separately; built-in Project workflows are unchanged.

## Implementation

The workflow observes requested **Checks** runs from pushes, using trusted code
from the default branch. It never executes feature code with the project token.
A global concurrency group with queuing serializes writes across branches.
API errors need inspection and a rerun; no periodic reconciliation is configured.
Renaming Checks or changing project fields requires updating this configuration.

```text
python -m unittest discover -s tests -p "test_project_status.py" -v
```

References: [GitHub issue branches](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-a-branch-for-an-issue)
and [Projects automation](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions).

# Merge audit: report-only

Branch protection, required reviews and rulesets are not available on this
private free-plan repository, so nothing blocks a merge without review. The
**Merge audit** workflow runs on every push to main. For each merged PR it
lists the approvals from someone other than the PR author and the check runs
on the PR head, and writes the result to the job summary with a warning per
flagged merge. An approval counts only if it was given on the commit that was
merged; an approval on an earlier commit is flagged, because the later
changes were not reviewed. A direct push without a PR is flagged too. Any API error makes
the row UNVERIFIED; it is never counted as reviewed. The job stays green: it
makes merges visible, it does not enforce anything.

The workflow token is read-only. To audit recent merges locally through `gh`:

```text
python .github/scripts/merge_audit.py --via-gh --recent 10   # 1 to 100
python -m unittest tests.test_merge_audit -v
```

There is no CODEOWNERS file. Code owners cannot be required on this plan, most
components have been changed by three or more people, and large consolidation
PRs cut across all of them. Revisit this if the team moves to a plan with
branch protection.
