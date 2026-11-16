# Marker instructions

[GROUP05_INSTRUCTIONS.md](GROUP05_INSTRUCTIONS.md) is the single source of the
submitted `GROUP05_INSTRUCTIONS.pdf`. Change the Markdown, never the PDF.

## Tagged command blocks

Every PowerShell or bash block is a step a rehearsal runs. Its fence carries a tag:

```text
powershell rehearse=B2 os=windows timeout=3600 needs=network,docker,cluster
```

| Key | Meaning |
| --- | --- |
| `rehearse` | Step id: a capital letter for the section and a number. Ids are unique and numbered upwards within each letter. |
| `os` | `windows` for PowerShell blocks, `linux` for bash blocks. |
| `timeout` | The rehearsal time limit in seconds, 1 to 14400. |
| `needs` | Optional: `network`, `docker`, `cluster`, `model` and `human` (a person must click, answer a prompt or press Ctrl+C). |
| `optional` | `yes` for steps that run only when something went wrong (troubleshooting, interrupted cleanup). |

Other blocks (`text`, `json`) are examples and take no tag. Section letters in
use: P tools, S Windows setup, A packaged runner, B connected harness, C agent
mode, L Linux, D security lab, E evidence, T troubleshooting, X cleanup.

## Shared text and claims

A line `<!-- include: docs/setup.md#required-tools -->` is replaced in the PDF
by that section of the named file, so the tool list is written once. Included
sections must not contain code blocks. Results are quoted through
`claim-<id>` blocks, which `scripts/evidence/summarize.py` fills from
[evidence/claims.json](../../evidence/claims.json) and checks with `--check`.

## Commands

```powershell
python scripts/instructions/extract_commands.py --check
python scripts/instructions/extract_commands.py --os windows --skip-optional
python scripts/instructions/build_pdf.py --version <tag> --repository-url <url> --members "<names>"
```

`--check` fails when a command block has no tag, a step id repeats or is out of
order, or a command names a script, a PowerShell parameter or a command-line
option that does not exist in the tree. CI runs it, so renaming a flag breaks
the build rather than a marker's run. The listing (`--format json` for tools)
is the input of a rehearsal; see the [rehearsal plan](rehearsal-plan.md).

`build_pdf.py` runs the same check, resolves the includes and writes
`dist/GROUP05_INSTRUCTIONS.pdf` with the standard-library renderer in
`scripts/instructions/pdf.py`; nothing needs to be installed. The cover shows
the version (from `--version` or a `VERSION` file), the source revision, the
repository URL and the team members. Values not given print as
"[to be added by the team]", and `scripts/submission/check.py` fails a PDF that
still contains that text. `--markdown` also writes the resolved Markdown to
`build/instructions/` for review.

The renderer covers the Markdown this file uses (headings, paragraphs, lists,
quotes, tables, code, inline code, bold and links) in the PDF base fonts, so
the source must stay within the Windows-1252 character set; the build names any
character it cannot print. Screenshots are not supported yet.
