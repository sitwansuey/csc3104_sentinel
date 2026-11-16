# Submission kit

Scaffolding for the milestone-5 deliverables. It supplies structure, checked
numbers and a readiness gate; the argument, the narration and the recording are
written and made by the team.

| Deliverable | Source here | Made by | Gate check |
| --- | --- | --- | --- |
| `GROUP05_INSTRUCTIONS.pdf` | [instructions source](../instructions/GROUP05_INSTRUCTIONS.md) | `scripts/instructions/build_pdf.py`, reviewed by the team | `instructions-pdf` |
| Research article | [article outline and claim map](article-outline.md) | The authors | `article-pdf`, `article-pages` |
| Video (10-15 min) | [storyboard](video-storyboard.md) and [recording checklist](recording-checklist.md) | The team records and narrates | `video-link`, `video-file` |
| Rehearsal records | [rehearsal plan](../instructions/rehearsal-plan.md) | Three tiers, one record each | `rehearsal:<tier>` |
| Release tag | Release work (roadmap item P2.09) | The repository owner | `release-tag` |

## Readiness gate

```powershell
python scripts/submission/check.py
python scripts/submission/check.py --video-file <local copy of the recording> --remote
python scripts/submission/check.py --format markdown
```

It reads [deliverables.json](deliverables.json), which the team keeps up to
date: the release tag, the article PDF path and page limit, the video link, the
required rehearsal tiers and the records. Each check prints PASS, FAIL or
MANUAL. The command exits with 1 while any check fails. It only reads: files,
`git rev-parse`, `git ls-remote` with `--remote`, and `ffprobe` with
`--video-file`.

MANUAL items are things only a person can do, such as reading the course's
generative-AI policy, confirming the deadline and limits with the lecturer,
giving markers access and uploading. They are listed under `manual` in
`deliverables.json`, always print as MANUAL and never pass automatically. Sign
them off outside the tool, for example in the submission pull request.

## Numbers in the article and video

Quote results only through claim ids from
[evidence/claims.json](../../evidence/claims.json), with their scope caveats.
For the article, generate LaTeX macros instead of typing values:

```powershell
python scripts/submission/claims_tex.py --verify --output article/generated/claims.tex
python scripts/submission/claims_tex.py --check-source docs/submission/article-outline.md
```

`--verify` refuses when `summarize.py --check` fails, so a stale value cannot
reach the article. `--check-source` reports any `\Claim...` macro or claim id
that `claims.json` does not define.
