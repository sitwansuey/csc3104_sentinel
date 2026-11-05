# Runner image security and validation

The previously exported laptop package is `sentinel-boutique-runner:0.3.0`, built
for `linux/amd64` from main `d94d5d8` plus the packaging changes recorded here.
Its base revision label identifies that main revision; use the exported matching
source ZIP and its checksum to reproduce that package. The later browser
injection controls, connected Boutique mode and service overview are source
updates; they are not included in that TAR or covered by its scan result.

## Docker Scout result

On 27 October 2026 (Singapore time), the final runtime image was scanned with
Docker Scout v1.27.0 using:

```powershell
docker scout cves --platform linux/amd64 --locations --format sarif --output .\data\sentinel\runs\image-build-20261027\release-runner.sarif.json --exit-code local://sentinel-boutique-runner:0.3.0
```

Scout indexed **137 packages and reported zero detected vulnerabilities**. The
command exited successfully and the raw SARIF contained zero results. No
severity, fixability, package or base-image exclusion flags were applied. No
custom VEX statements or Scout exceptions were introduced.

Image identity:

```text
sha256:739de92733f1c532cfeedee9a119b59115e7f5b208675d47faeeb8129b698f1a
```

This is a point-in-time result for the final runner image, not a guarantee of
absence of vulnerabilities. It does not certify Docker Desktop, the host OS,
build stages, kind node, grpcurl or the separate upstream shippingservice image.
It does not replace authentication, approval security or runtime review. Scan
again before sharing a later rebuild; the scanner database and packages change.

## Changes made

- Use the official Python 3.12 Alpine 3.23 base, pinned by digest, and update
  its installed packages. Retain the CA trust bundle and verify it exists.
- Remove unused pip/setuptools/wheel and BusyBox/shell maintenance packages from
  the final image. Removing a package from Alpine's world list alone was not
  sufficient: shell-dependent maintenance packages also had to be removed.
  The Dockerfile explicitly checks BusyBox is absent. `/tmp` is recreated with
  mode 1777 after package removal; the launcher mounts its bounded tmpfs there.
- Build the unchanged kubectl CLI entrypoint from Kubernetes v1.37.1 commit
  `f78e722310e50bcaca9276be22276d9e91d91308` against published client modules
  `k8s.io/kubectl`, `k8s.io/component-base` and `k8s.io/client-go` v0.37.1.
  Kubernetes documents these client packages as independent of its server module.
- Compile with Go 1.27.3, `golang.org/x/net` v0.60.0 and `golang.org/x/text`
  v0.42.0. The result is a locally rebuilt client with dependency patches,
  not the unchanged upstream prebuilt binary. Keep the Kubernetes license.
- Keep the Node build/test stage separate. The runtime has no Node/npm, Docker
  CLI/socket, kind, BusyBox or interactive shell. It executes Python and kubectl
  directly as UID 10001, with the existing read-only-root/capability restrictions.
- Include the repository's project-status script so the same complete offline
  suite can run inside the image; no GitHub automation credentials are embedded.

The ordinary Alpine 3.24 candidate retained a musl finding. The Alpine 3.23
candidate retained BusyBox CVE-2025-60876 until that unused package was removed.
An initial Kubernetes root-module build also produced ambiguous root-module
version attribution/server advisories. The final client-only build retains
normal module metadata for its actual client dependencies; metadata is not
stripped to conceal findings. Raw candidate reports/logs remain in the ignored
`data/sentinel/runs/image-build-20261027/` folder.

References: [Docker build guidance](https://docs.docker.com/build/building/best-practices/),
[Go security release history](https://go.dev/doc/devel/release),
[upstream kubectl entrypoint](https://github.com/kubernetes/kubernetes/blob/v1.37.1/cmd/kubectl/kubectl.go),
[published kubectl client packages](https://github.com/kubernetes/kubectl).

## Checks performed

| Check | Result |
| --- | --- |
| Current source, Windows/Python 3.15 | 687 tests passed |
| Shared contract examples | Six fixtures passed; no cluster contacted |
| Dashboard on Windows | Type-check/build and 67 tests passed |
| Dashboard inside native Node build stage | Type-check/build and 67 tests passed |
| Final runtime, Linux/Python 3.12 | 687 tests passed after `/tmp` correction |
| PowerShell syntax and existing lab Bash syntax | Passed |
| Final runtime Docker Scout gate | Zero findings / 137 detected packages |

Python tests use offline dependencies and local test HTTP servers. Their output
may contain simulated PASS messages; those are not live Kubernetes evidence.

## Actual packaged Kubernetes validation

Run `data/sentinel/runs/packaged-boutique-20261027-004815-7b4d32/` used the exact final image
above with `scripts/demo-start.ps1 -SkipBuild -Unattended`. It created a fresh
real kind cluster and completed all **13 checkpoints**, exiting with code 0.

- Healthy baseline: actual upstream shippingservice image and real GetQuote
  requests; actual selected-Deployment get/patch RBAC, forbidden write denied.
- Missing approval, hard policy rejection and stale proposal: zero Executor
  mutations. The server-side resourceVersion race and competing-worker checks
  passed. Approved rollback used one repair write; duplicate replay and changed
  intent under a reused ID did not repeat it.
- A real worker exited after one successful patch. Restart made one read and
  **zero additional write attempts**, reconciling the actual committed result.
- Durable Role 6 stored streamed evidence in `verifier.sqlite3`. Both actual
  recovery rounds produced passing RECOVERED outcomes and Coordinator closure.
- The final unnecessary rollback was REJECTED, with zero repair patch attempts.
- The dashboard's built frontend and snapshot endpoints returned HTTP 200.
  Assertions checked three incidents, two APPLIED actions, two recovery outcomes,
  one rejection, three dependency results, seven Guard reports and 44 cluster
  snapshots. This run checked HTTP/data integration, not browser rendering.
- The wrapper removed the runner and named cluster. Both operator and restricted
  Executor credentials were removed. Dashboard collector/server stopped.

The image TAR was exported and loaded back on this desktop with the same image
identity. These checks do not substitute for a rehearsal on the actual laptop.
Interactive Enter/Ctrl+C handling was not rerun for this image; this validation
used the unattended path. The existing frontend build emits a non-failing large
bundle warning; no dashboard source was changed to suppress it.

## Host changes and limitations

Docker Desktop was started; images/build layers and ignored run artifacts were
created. `npm ci` installed project-local `dashboard/node_modules` and used the
normal npm cache. No new system-wide applications were installed. Go, Alpine
package operations and client compilation occurred inside Docker build stages.

The runtime still has trusted operator credentials for lab setup/injection and
separate restricted Executor credentials. This is credential separation inside
one trusted orchestrator, not isolation against compromise of that orchestrator.
Coordinator and Executor coordination remain local; cross-host fencing is not
implemented. Planner remains a harness substitute. The exported Windows/amd64
image does not establish Apple Silicon, macOS, cloud or second-laptop success.

Use [the laptop quickstart](laptop-demo-quickstart.md) for transfer and rehearsal.
