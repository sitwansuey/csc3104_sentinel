# Project status automation

## Team usage

1. Add the issue to [Sentinel Project 2](https://github.com/orgs/sitwansuey/projects/2)
   and put it in **Todo**.
2. Start from current `main`. Name the branch `feat/21-verifier-live-evidence`
   (replace 21 with the issue number). Also supported: `feature`, `fix`, `bugfix`,
   `chore`, `docs`, `test`, and `refactor`, followed by `/NUMBER-description`.
3. Push actual work. When the existing **Checks** workflow is requested for that
   push, **Project status** changes Todo to **In Progress**. A local commit cannot
   trigger GitHub Actions until pushed. A branch created at main or containing
   only merge commits does not count as implementation.
4. The automation fills an empty **Start date** from the first non-merge commit
   unique to that branch compared with current main, using its committer date in
   Singapore time. Existing historical dates are preserved. Use one issue per
   branch; do not stack unrelated feature work if relying on this calculation.
   History exceeding the comparison API limit fails explicitly; set that date
   manually. Already-merged history cannot be reconstructed from this comparison.
5. Link the PR with `Closes #21` in its description, or use the numbered branch.
   On an open PR, **in progress** moves its open, same-repository closing issues
   and numbered-branch issue to **In Progress**. **ready to merge** moves them to
   **Ready To Merge** when the PR is not a draft. Its own card moves too if already
   in the project. If both labels are present, **in progress wins**, allowing a
   mistaken ready decision to be corrected immediately.

| Current PR labels | Project status |
| --- | --- |
| in progress (with or without ready to merge) | In Progress |
| ready to merge only, non-draft PR | Ready To Merge |
| Neither, after this automation managed a label | Restore saved Todo or Backlog |
| Neither, without previous label tracking | Leave the card unchanged |

Removing one label follows the remaining label. Removing the final status label
restores each card's own previous queue. The **Automation state** text field
stores that queue and controlling PR before a label changes the status. A push
also records Todo before starting work. For older active cards without saved
history, the fallback queue is Todo. Do not edit this bookkeeping field manually;
it can be omitted from normal views. No Start date is cleared or overwritten by
label changes. A fresh push after returning to Todo can start work again.

No extra PR cards are created. Done cards and closed issues are left alone;
explicit PR labels can activate Backlog cards. Further pushes without a label
change do not move Ready To Merge backwards. Use one controlling PR per issue;
another PR cannot overwrite or remove the first PR's saved label control.
A label indicates a human decision; this automation does not approve, merge or
check that reviews/CI have passed. Existing Done automation is not changed.
Missing project cards must be added manually.

## One-time credential setup

The repository's normal `GITHUB_TOKEN` cannot update an organization Project.
An administrator must supply an additional Actions secret named `PROJECT_TOKEN`.
Do not paste a token into an issue, chat, workflow file or commit.

A fine-grained personal access token can be scoped to resource owner
**sitwansuey**, repository **csc3104_sentinel**, with organization **Projects:
read/write** and repository **Contents, Issues, Pull requests, Actions: read**.
The token owner must already have write access to the Project. Select an expiry
covering the project duration, and obtain organization approval if required.
For organization-managed credentials, a GitHub App is an alternative that needs
an installation-token step before this workflow's Python step.

Save the credential under repository **Settings → Secrets and variables →
Actions → New repository secret**, named `PROJECT_TOKEN`. Never reuse an unrelated
token or copy an existing local login credential into the repository automatically.

Merge this workflow into main after review, then test a normal issue branch push
and a PR label change. The default-branch workflow can observe Checks runs from
older feature branches without executing their code. Missing credentials make
the status job fail with setup instructions; the existing Checks workflow remains
independent. Before credentials and a merged workflow are available, only the
automated fake-API tests have been run, not a real event-driven board update.

## Implementation and checks

`.github/workflows/project-status.yml` handles the requested Checks runs and
PR metadata events. It checks out only the default branch; PR text and feature
code are never executed with the project credential. The script uses fixed
repository/project/field IDs, bounded API requests, paginated project items and
current PR state. It handles both label addition and removal using current labels,
not stale event contents. Push handling also reconciles current PR labels so queued event
replacement does not silently lose a ready label. Issues can still start work
against agreed interfaces while blocked; blockers are not automatically removed.

```text
python -m unittest discover -s tests -p "test_project_status.py" -v
```

The existing CI also discovers these tests. They use fake APIs without tokens or
real project writes. For credential and trigger details, see GitHub's
[Projects automation guide](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/automating-projects-using-actions)
and [workflow events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows).
