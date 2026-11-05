# Existing runner and future image release

The source launch is the current connected workflow. The older packaged runner
and host bootstrap remain for the smaller shipping-only harness experiment:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1
```

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

The launcher still names `sentinel-boutique-runner:0.3.0`; old local images and
the exported 0.3.0 TAR/source ZIP were retired. **No newly built/scanned image
from this integrated/cleaned source has been accepted.** Do not use `-SkipBuild`
or `-Offline` until a new matching image has been built and rehearsed.
Full connected/agent packaging is not supplied by the old bootstrap.

For a future release, build from the reviewed revision, record architecture and
digest, scan the runner and separately assess workload/node images, then export
with `docker save --output <runner.tar> <tag>`. Share its matching source revision
and checksums. The recipient verifies with `Get-FileHash`, runs
`docker load --input <runner.tar>`, checks `docker image inspect` architecture,
then rehearses online and cached startup on the actual laptop.

The earlier 27 October scan reported zero findings for the exact historical
amd64 digest `739de92733f1c532cfeedee9a119b59115e7f5b208675d47faeeb8129b698f1a`.
Its scan/checksum records remain local historical evidence. That result says
nothing about the new source/build or future CVEs. No new macOS/Apple Silicon,
whole-platform image or security guarantee is claimed.
