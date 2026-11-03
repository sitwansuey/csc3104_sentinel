# Run the packaged demo on a Windows laptop

This guide targets a Windows laptop with an Intel/AMD CPU and Docker Desktop's
Linux container engine (`linux/amd64`). It runs the current single-service
Boutique experiment, including two recoveries, durable Role 6 verification and
a final rejected unnecessary rollback. Planner proposals are harness-authored.

## 1. Prepare the laptop

Install Docker Desktop and kind, start Docker Desktop, and finish its first-run
setup. Docker Desktop's built-in Kubernetes does not need to be enabled. Use
PowerShell for the following commands:

```powershell
docker version
docker info --format '{{.OSType}}/{{.Architecture}}'
kind version
```

The Docker server must be reachable and report Linux on x86_64/amd64.
The separate Windows dependency installer is optional:
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1`.
It also installs tools for host development; those extra tools are not required
for the packaged route. The image supplies Python, kubectl and the built UI;
it does not install Docker Desktop or kind on the host.

## 2. Copy the matching source and image

Copy the exported source ZIP, runner TAR and `SHA256SUMS.txt` from the desktop's
`runs/exports/` folder. Extract the source ZIP, keep the TAR beside the extracted
`sentinel-boutique-source` folder, and open PowerShell in that source folder.
Use this matching source snapshot when reproducing the exported image. Current
GitHub source also includes later dashboard/connected-mode updates that are not
in that TAR; use the [connected source guide](boutique-connected.md) for those.

Verify the downloaded files against the supplied checksums, then load the image:

```powershell
Get-FileHash ..\sentinel-boutique-runner-0.3.0-linux-amd64.tar -Algorithm SHA256
docker load --input ..\sentinel-boutique-runner-0.3.0-linux-amd64.tar
docker image inspect sentinel-boutique-runner:0.3.0 --format '{{.Os}}/{{.Architecture}} {{.Id}}'
```

Loading the image restores its built contents; no source build is necessary.
To build from matching source instead, omit `-SkipBuild` in the next step. The
first source build compiles kubectl and needs network access and additional time.

## 3. Create the environment and run the demo

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -SkipBuild
```

The host script downloads the node/workload/probe images, creates
`sentinel-boutique-package`, loads immutable image references, and starts the
runner with protected Kubernetes credentials. The runner deploys shippingservice,
measures the baseline and serves the dashboard. First startup needs internet.

Open **http://127.0.0.1:8765** and wait for the baseline-ready prompt.

1. Press **Enter** to inject the invalid PORT and run detection, Guard, approval,
   restricted rollback and Role 6 verification.
2. At the second prompt, press **Enter** for the fault plus Executor crash after
   its Kubernetes write. Restart must reconcile without another repair write.
3. At the final prompt, press **Enter** to inject a false recovery recommendation
   against the healthy service. Guard must reject the unnecessary rollback.

Expected final state: **two RECOVERED incidents, two APPLIED executions, two
passing recovery outcomes, and one REJECTED false alarm with zero repair patches**.
The final scenario does not deliberately break shippingservice again.

For a completely automatic run that exits and cleans up after its assertions:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -SkipBuild -Unattended
```

The runner uses `hipstershop.ShippingService/GetQuote` at
`shippingservice:50051` through temporary grpcurl Pods. Kubernetes API calls
inspect/repair configuration; gRPC requests measure application behavior.

## 4. Optional second terminal: watch or manually inject

No host kubectl installation is required for this watcher; it uses kubectl
already present inside the kind node. Run after the node and namespace exist:

```powershell
docker exec sentinel-boutique-package-control-plane kubectl --kubeconfig=/etc/kubernetes/admin.conf -n sentinel-boutique get pods -w
```

For a manual first fault, wait for the first baseline-ready prompt, then run this
in terminal 2 **before** starting the watcher above:

```powershell
docker exec sentinel-boutique-package-control-plane kubectl --kubeconfig=/etc/kubernetes/admin.conf -n sentinel-boutique set env deployment/shippingservice --containers=server PORT=invalid-port
```

Inspect the failure, then press Enter in terminal 1. The harness reasserts the
same fault and begins detection/recovery. It is paused before that point and is
not an always-running recovery controller. Let subsequent prompts inject their
faults automatically; do not change configuration during recovery.

## 5. Clean up

After inspection, stop the watcher with Ctrl+C in terminal 2, then press Ctrl+C
in terminal 1 and let its cleanup finish. The unattended run cleans up itself.
If a run was interrupted or deliberately retained:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-stop.ps1
kind get clusters
docker ps --filter name=sentinel-boutique-runner
```

The dedicated cluster and runner should be absent. Evidence remains under
`runs/packaged-boutique-*/data/evidence/`, including `verifier.sqlite3`, outcomes,
execution records and dashboard validation. Image caches and Docker Desktop
remain. If the launcher was force-closed, inspect that run's `bootstrap` folder
for a leftover `operator-kubeconfig.yaml`; remove that exact credential file after
stopping its resources. Do not share credentials or unsanitized application logs.

## 6. Rehearse before presenting

Complete the online run on the actual laptop, then check cached startup:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -Offline -Unattended
```

`-Offline` skips build/pull commands and requires all four images already cached.
It is not proof of operation with network connectivity disabled. The desktop's
test cannot replace this second-machine rehearsal.

See [the detailed package guide](packaged-demo.md) for runtime boundaries, and
[image security and validation](image-security.md) for the exact scan scope and
recorded checks. A clean runner scan does not scan the kind node or workload
images, and is not a guarantee against future vulnerabilities.
