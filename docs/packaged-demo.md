# Packaged Boutique demo on a Windows laptop

This runs the existing real Boutique recovery experiment from a Docker image.
You need Docker Desktop with a running Linux engine, kind and PowerShell on the
host. Host Python, Node and kubectl are not needed for this packaged route.
The image contains Python 3.12, kubectl and the prebuilt dashboard. Node is used
only in its build stage. Roles 1, 2, 4 and 5 run inside the demo container;
shippingservice and request-probe Pods run on actual Kubernetes inside kind.
Roles 3/6 remain operator/test substitutes, not implemented services.

## Build, create, inject and inspect

From the repository root, with Docker Desktop running:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1
```

The first run needs internet and builds the image, runs the frontend tests,
downloads workload/node images, creates a dedicated kind cluster and starts the
packaged experiment. Docker layer caching makes subsequent builds faster.

Open `http://127.0.0.1:8765`, or the URL printed by the script. Once healthy quote
measurements and the baseline are captured, press Enter in the main terminal to
inject the first bad PORT rollout. Watch the UI's Kubernetes, Monitoring,
Coordinator, Recovery Guard and Executor views. Press Enter at the second prompt
for the real post-write worker-crash experiment. The test suite runs actual
refusal, concurrency, duplicate and crash/reconciliation checks.

After successful checks the UI stays live for inspection. Press Ctrl+C in the
main terminal after the demo; normal cleanup stops the UI/collector and removes
the dedicated runner/cluster. Let the wrapper finish instead of closing its window.

For automatic execution without prompts or a persistent UI:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -Unattended
```

Useful options:

| Option | Behaviour |
| --- | --- |
| `-SkipBuild` | Use the existing runner image deliberately; rebuild after source changes |
| `-Offline` | Skip build and registry pulls; require all four images already cached locally |
| `-Port 8766` | Publish the UI at a different host loopback port |
| `-KeepCluster` | Retain Kubernetes after the runner stops; temporary administrator credentials are still removed |

Existing resources with the reserved names are refused at startup. Inspect them
or use the explicit stop command before rerunning. This avoids silently reusing
a partially failed demo or another experiment's state.

## Stop and cleanup

Normal foreground completion cleans up automatically. From another terminal,
or after an interrupted/retained run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-stop.ps1
```

This stops only the ownership-labelled `sentinel-boutique-runner` and deletes
`sentinel-boutique-package`. It leaves the original `sentinel-boutique-lab`,
`sentinel-lab`, unrelated containers, Docker Desktop and image caches alone.
Runner SIGTERM is handled as a graceful interruption: the Python cleanup attempts
to stop its threads, remove restricted credentials and retain the summary.
Do not interrupt bootstrap while binary image export/import is running.

Evidence is retained under:

```text
runs/packaged-boutique-<timestamp>-<id>/
  package-session.json        image identity, published URL and cluster
  host-cleanup.json           host teardown results
  bootstrap/environment.json  reviewed image digests and fixed cluster scope
  data/evidence/              normal Boutique records and dashboard evidence
```

`data/evidence/` contains baselines, incidents, Role 2 state/outbox, Coordinator
and execution journals, Guard reports, proposals/approvals, actual quote samples,
transport counters and dashboard checks. It survives runner removal. Private
executor credentials are removed by Python; the host bootstrap's operator
kubeconfig is removed after the runner stops. Force-closing the host process can
skip host cleanup: run the stop script, then check the printed run's `bootstrap/`
for a leftover `operator-kubeconfig.yaml` and remove that exact file. Evidence and
image caches are intentionally not deleted or globally pruned.

## What each file does

| File | Responsibility |
| --- | --- |
| `infra/demo-runner.Dockerfile` | Multi-stage UI build/test and non-root Python/kubectl runtime |
| `.dockerignore` | Keeps Git history, credentials, run evidence and host build caches out of the build context |
| `scripts/demo-start.ps1` | Host build/preparation, new cluster, image import, internal kubeconfig, mounts/port and foreground run |
| `scripts/packaged-demo-common.ps1` | Fixed names, tool lookup, digest checks, binary-safe imports and scoped teardown |
| `scripts/demo-stop.ps1` | Explicit teardown entrypoint |
| `scripts/test_boutique_live.py` | Same live experiment, with trusted prepared-environment mode that invokes no Docker/kind commands |
| `scripts/boutique_dashboard.py` | Same UI and telemetry lifecycle; explicit container bind |
| `runtime/dashboard.py` | Read-only HTTP API; host default remains loopback |

The runtime is non-root, has a read-only root filesystem, dropped capabilities,
a bounded writable `/tmp`, and one writable evidence mount. The bootstrap mount
is read-only. It has no Docker socket, Docker CLI, kind, Node or npm. The trusted
experiment still needs administrator Kubernetes credentials for setup, injection
and measurement. Actual executor operations use a separate restricted kubeconfig.
This is credential separation inside a trusted demo container, not process-level
isolation from a compromised administrator harness.

## Networking and persistence

The runner joins Docker's `kind` network and uses kind's internal API endpoint
`https://sentinel-boutique-package-control-plane:6443` with the cluster CA and
client credentials. TLS verification remains enabled. It does not rewrite the
endpoint to a host loopback address or use insecure TLS flags.

Only the container UI opts in to `0.0.0.0`; Docker publishes it on host
`127.0.0.1` only. Other host-run dashboard commands keep their loopback default.
There is one local runtime owner and a shared journal/lock directory. This does
not add distributed leases or cross-host coordination.

Cached workload images are streamed as binary tar into the node's containerd
with the selected architecture. This avoids multi-platform import errors and
does not require a registry connection from the Kubernetes node. Both workload
and client use recorded immutable manifest references with imagePullPolicy Never.

## Move to the laptop or share with the team

Option A: copy/clone this version of the repository on the laptop and run
`demo-start.ps1`. Docker builds everything; host Python/Node are unnecessary.

Option B: transfer the already-built runner image plus the matching repository
version. On the original machine:

```powershell
docker save --output .\runs\sentinel-boutique-runner.tar sentinel-boutique-runner:0.1.0
```

Copy that tar separately. On the laptop:

```powershell
docker load --input .\sentinel-boutique-runner.tar
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -SkipBuild
```

This avoids rebuilding the runner but still downloads missing node/workload
images. The tag identifies this demo release; `package-session.json` records the
actual image ID. Do not mix a newer wrapper with an incompatible older image.

Before presenting without venue internet, first complete an online rehearsal on
that laptop, then run `demo-start.ps1 -Offline -Unattended` to verify cached
startup and image loading. Skipping pulls is not proof that the machine has no
network access; a fully disconnected second-machine rehearsal is a separate check.
This implementation targets Windows PowerShell and Linux containers. A second
laptop and ARM architecture require their own validation; no cross-platform
success is inferred from the current desktop's results.

For the recovery semantics and individual role responsibilities, see the
[Boutique instruction book](boutique-demo-handbook.md). The packaged path changes
the environment boundary, not the Guard/executor algorithm. APPLIED still routes
to VERIFYING and then RECOVERED after Role 6 consumes and verifies live evidence.
It does not claim AI planning, automatic dependency discovery or cloud readiness.

## Recorded packaged validation

On 26 October 2026 (Singapore time), the runner image was built on Windows
Docker Desktop using Linux/amd64 containers. The image ID recorded by the runs
was `sha256:6280244e5093ec49789cf56a1aa89b014e8bbb1f7a45444d24ecea51173ee6b8`.

| Check | Recorded outcome |
| --- | --- |
| Multi-stage image build | Passed, including UI build and all 54 frontend tests inside the build stage |
| Offline suite inside built Python 3.12/Linux image | 506 tests passed; this build preceded three additional packaging-boundary tests |
| Current source on Windows/Python 3.15 | 509 tests passed, including the three packaging-boundary checks |
| Packaged live recovery | Passed on a newly created real kind cluster; full Role 2 evidence/outbox, actual Guard and restricted executor |
| Cached startup | `-Offline -Unattended` created a fresh cluster and passed without build/registry-pull commands |
| Interactive presentation | Both Enter prompts, completed UI inspection hold and Ctrl+C cleanup passed in a real terminal |
| Host-published browser UI | Headless installed Edge rendered Overview, Recovery Guard, Executor and Kubernetes, with zero page JavaScript errors |
| Idempotency/concurrency | Duplicate replay, changed-ID payload refusal, competing process and server-side version race passed |
| Post-write crash | One actual patch before exit 73; restart used one read and zero mutations |
| Lifecycle/health (historical image) | Both incidents VERIFYING before Role 6 integration; new source builds require two real RECOVERED outcomes |
| Cleanup | Runner and named cluster removed; host operator and restricted executor credential files removed; evidence retained |
| Runtime boundary | Inspected non-root UID 10001, read-only root, dropped ALL capabilities, read-only bootstrap mount, writable data mount, kind network and host-loopback UI publication; Docker/kind/Node/npm absent |

Online-prepared run: `runs/packaged-boutique-20261026-014835-a3aa07/`.
Cached/browser run: `runs/packaged-boutique-20261026-021759-2f6ab0/`.
Interactive/boundary run: `runs/packaged-boutique-20261026-022320-26a851/`.
These directories are ignored local evidence. The browser run includes
`browser-validation.json` and UI screenshots; both include the normal experiment
evidence under `data/evidence/` and `host-cleanup.json`. Initial unsuccessful
bootstrap attempts are retained separately and are not counted as recovery success.
The interactive run additionally retains `runtime-boundaries.json`.

The cache test skipped pulls; it was not a network-disconnected laptop test.
Export/load on a second machine, ARM builds and cloud deployment were not tested.

## Live Role 6 integration

Rebuild the runner from the updated source to include Role 6; loading an older
image does not update it. The new runner calls the actual dependency evaluator,
collects protocol-native gRPC evidence, consumes verifier.request and publishes
formal outcomes. Successful current-source runs finish with two RECOVERED
incidents. Historical image results above remain unchanged. See
[current-system-demo.md](current-system-demo.md) for the full flowchart.
