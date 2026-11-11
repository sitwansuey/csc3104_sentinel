# Rehearsal plan

A rehearsal follows [the instructions](GROUP05_INSTRUCTIONS.md) on a machine
that has never run Sentinel, and records what happened. It shows that a
stranger can run the release, not only the machine it was developed on.

**Status: planned, not run.** No rehearsal record exists yet. The submission
gate (`scripts/submission/check.py`) fails until each tier below has a passing
record for the release revision.

## What is rehearsed

The tagged command blocks of the instructions, in document order, exactly as
written. List them with:

```powershell
python scripts/instructions/extract_commands.py --os linux --skip-optional --format json
python scripts/instructions/extract_commands.py --os windows --skip-optional
```

Each step has an id, a time limit and what it needs. Steps marked
`optional=yes` are run only when a problem calls for them, and then recorded.
Steps that need `human` are done by the person rehearsing; in an automated tier
they are recorded as not run. A step passes when its commands exit with code 0
within the time limit and its output matches what the instructions say to expect.

Rehearse a tagged commit: the release candidate's tag, with the PDF built from
it (`scripts/instructions/build_pdf.py --version <tag>`). A rehearsal of an
untagged revision is practice, not a record.

## Tier 1: Linux clean room in a container on the development host

Purpose: run the Linux steps against an empty image cache and no kubeconfig.

- Start a privileged `docker:dind` container (pinned by digest) with its own
  Docker daemon, with kind, kubectl, Python 3.12 and Node installed at the
  versions in [setup](../setup.md#required-tools). Cap its memory and CPU below
  what Docker Desktop has, so other workloads on the host keep running.
- Give it the source as a `git bundle` of the tag, mounted read-only. No GitHub
  credentials, host kubeconfig, `~/.docker` or SSH keys enter the container.
- Clone from the bundle, then run the `os=linux` steps in order with their time
  limits. kind inside the container uses the inner Docker daemon, so its
  clusters cannot collide with clusters on the host.
- Capture the transcript, each step's duration and exit code, `docker system df`
  before and after (bytes downloaded on a cold cache), the run's
  `summary.json` and the final status.
- Always remove the container at the end, also after a failure.
- Before and after, run `kind get clusters` on the host and confirm that the
  list is unchanged.

Not covered: Windows, Docker Desktop, and steps that need a person (L5). kind
inside Docker-in-Docker needs privileged mode and cgroup v2; if it does not
start, record that as a FAIL with the error rather than changing the host.

Tooling still to build: a pinned rehearsal image (`deploy/rehearsal/Dockerfile`)
and a driver (`scripts/submission/rehearse.py`) that runs the listed steps,
enforces the time limits, always removes the container and writes the record.
Its tests should assert the `docker run` arguments mount no host kubeconfig or
`~/.docker`.

## Tier 2: a fresh Windows user or virtual machine

Purpose: run the Windows steps as written, with no tools or caches left over
from development.

- Prefer a new virtual machine with Windows 11 amd64 and nested virtualisation,
  so Docker Desktop and WSL 2 can run. A new local Windows user on the
  development machine is weaker: Docker Desktop's engine, its images and its
  clusters are shared between users, so record which images were already
  cached instead of deleting them.
- Follow the PDF from section 2. Run steps P1, S1-S5, A1, B2, C1 and C3 (C
  only if the machine can hold the model), E1, E2 and X1, then the browser run
  B1 by hand.
- Do not delete clusters or images the instructions did not create. On a shared
  machine, list `kind get clusters` before and after and confirm that clusters
  you did not create are unchanged.

## Tier 3: a teammate's laptop

Purpose: a person who has not set the project up before follows the PDF
literally on their own Windows laptop.

- They use only the PDF. If they get stuck, they write down where and why
  before asking for help, and the help given is recorded as a deviation.
- They record the time per section, every deviation, error message and question,
  and the outcome of Paths A, B and C (C only with a suitable GPU).
- Screenshots may be attached after checking them for tokens, user names and
  home paths.

## Exit criterion

The instructions are ready to submit when every tier listed in
`docs/submission/deliverables.json` has a PASS record on the release revision.
Each deviation found becomes a fix to the instructions or the scripts and a new
release candidate, which is rehearsed again.

## Record

One JSON file per rehearsal, named `<date>-<tier>.json`, kept in
`docs/submission/rehearsals/` and listed under `rehearsal_records` in
`docs/submission/deliverables.json`. Describe the machine by class only: no
serial numbers, host names, user names or home paths.

```json
{
  "schema": "sentinel-rehearsal-v1",
  "tier": "linux-container",
  "outcome": "PASS",
  "source_revision": "<full commit id of the release tag>",
  "instructions_version": "<tag>",
  "date": "<YYYY-MM-DD>",
  "machine": "<class only, for example: Windows 11 amd64 laptop, 16 GB RAM, Docker Desktop>",
  "steps": [{"step": "L1", "result": "PASS", "seconds": 0, "note": ""}],
  "not_run": ["L5"],
  "deviations": []
}
```

`tier` is one of `linux-container`, `windows-fresh`, `teammate-laptop`.
`outcome` and each step's `result` are `PASS` or `FAIL`; a step that was not
run goes in `not_run`. `scripts/submission/check.py` reads these fields.
