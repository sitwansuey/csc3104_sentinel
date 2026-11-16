# Existing runner and future image release

The source launch is the current connected workflow. The older packaged runner
and host bootstrap remain for the smaller shipping-only harness experiment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1
```

Without flags the runner starts interactively (`docker run -it`) and pauses
before each injection, so it needs a terminal. Options:

| Flag | Effect |
| --- | --- |
| `-Unattended` | No terminal; run every scenario with automated lab approval (`auto-lab`, recorded as automated, never as a person) and exit after the checks |
| `-UIControls` | Start the scenarios from the dashboard buttons instead (not with `-Unattended`). The run still attaches a terminal (`docker run -it`), because approval is typed there; there is no unattended UI-controls mode |
| `-GuardRejection` | Run only the unnecessary-rollback rejection scenario |
| `-KeepCluster` | Keep the dedicated cluster after the run |
| `-Port <n>` | Dashboard port on 127.0.0.1 (default 8765) |
| `-SkipBuild`, `-Offline` | Reuse an existing image; see the caution below |

`deploy/docker/demo-runner.Dockerfile` builds the UI and a restricted non-root
Python/kubectl runtime. `scripts/demo-start.ps1` builds/prepares the dedicated
kind cluster, imports reviewed images, supplies temporary bootstrap credentials
and runs the experiment. `packaged-demo-common.ps1` owns shared preparation and
teardown; `demo-stop.ps1` is the explicit interrupted-cleanup entrypoint:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-stop.ps1
```

The runtime uses a read-only root filesystem, dropped capabilities, temporary
writable storage, an evidence mount and a read-only bootstrap mount. It has no
Docker socket or Docker/kind/Node/npm runtime tools. Actual repair still uses
separate restricted Executor credentials. The trusted setup harness's admin
credentials are not process-level isolation from that harness.

The launcher names `sentinel-boutique-runner:0.3.0` and builds it from your
checkout; old local images and the exported 0.3.0 TAR/source ZIP were retired.
Committed packaged runs:

<!-- BEGIN GENERATED:claim-c-packaged -->
> **C-PACKAGED:** The packaged runner image completed the Boutique walkthrough in 5/9 attempts.
> Scope: Attempts include image-build-only and interrupted starts. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-packaged -->

For PR #98 (merged 11 November 2026) the runner was rebuilt with
`golang.org/x/net` v0.62.0, scanned with the project's pinned Trivy (0 High,
0 Medium, one unrated Go finding reported in the PR) and passed
`demo-start.ps1 -Unattended` with 13 of 13 checks. That scan and run are
reported in the PR, not committed as evidence bundles. There is no tagged or
published release image yet (`L-NO-RELEASE` in [limitations](limitations.md)).
Use `-SkipBuild` or `-Offline` only with an image you built from the same
revision. Full connected/agent packaging is not supplied by the old bootstrap.

The [release process](release.md) automates the following from a version tag;
it has not run yet. For a future release, build from the reviewed revision,
record architecture and digest, scan the runner and separately assess
workload/node images, then export with `docker save --output <runner.tar> <tag>`. Share its matching source revision
and checksums. The recipient verifies with `Get-FileHash`, runs
`docker load --input <runner.tar>`, checks `docker image inspect` architecture,
then rehearses online and cached startup on the actual laptop.

An earlier 27 October scan of a historical amd64 digest
(`739de92733f1c532cfeedee9a119b59115e7f5b208675d47faeeb8129b698f1a`) reported
zero findings. Its records are local only and say nothing about later builds.
No new macOS/Apple Silicon, whole-platform image or security guarantee is claimed.
