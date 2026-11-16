# Sentinel: installation and running instructions

This file is the single source of the submitted `GROUP05_INSTRUCTIONS.pdf`.
`scripts/instructions/build_pdf.py` builds the PDF from it; do not edit the PDF
by hand. How the source works is described in [the instructions README](README.md).

## How to read these instructions

- Run every command from the repository root unless a step says otherwise.
- Every command block is a numbered step. Its tag gives the step id, the
  operating system, the time limit used in a rehearsal and what the step needs.
  The PDF prints the tag above the block, for example
  "Step B2 | windows | up to 60 min | needs network, docker, cluster".
- "needs human" means the step waits for you: a browser click, an installer
  prompt or Ctrl+C. "only if needed" marks a step for when something went wrong.
- A rehearsal runs these same tagged blocks, in order, on a clean machine. See
  the [rehearsal plan](rehearsal-plan.md) for the three rehearsal tiers and
  [the submission README](../submission/README.md) for where the records are kept.
- A long command in the PDF is wrapped with the shell's own continuation
  character (a backtick in PowerShell, a backslash in bash), so it still runs
  when copied.

## 1. What Sentinel is

Sentinel diagnoses Kubernetes incidents and checks recovery proposals before a
restricted Executor applies them. Kubernetes then reconciles the repaired
configuration, and Sentinel's Verifier decides on its own whether service health
recovered. **APPLIED does not mean RECOVERED.**

The lab runs Google Online Boutique (eleven Deployments) in a dedicated kind
cluster on your machine. The harness breaks the shipping service's
configuration, and Sentinel's six roles take over: the Observer detects the
failure, a proposal is made (by a fixed harness or, in agent mode, by a local
language model), the Guard checks it, the Executor applies it with restricted
credentials and the Verifier measures whether the service is healthy again.
Model output never grants permission to change the cluster.

These results are committed as evidence. Each one is quoted with its claim id
and scope, and is regenerated from `evidence/claims.json`:

<!-- BEGIN GENERATED:claim-c-connected-harness -->
> **C-CONNECTED-HARNESS:** The connected Boutique walkthrough (two verified recoveries, crash reconciliation and a refused unnecessary rollback) passed in 4/4 runs.
> Scope: One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. Harness-authored proposals, no model. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-connected-harness -->

<!-- BEGIN GENERATED:claim-c-agent-warm -->
> **C-AGENT-WARM:** qwen3:14b agent mode reached RECOVERED in 3/3 consecutive runs after the target-name fix.
> Scope: Three runs only, chosen as the latest consecutive set; earlier agent runs include failures (see C-AGENT-ALL). One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-agent-warm -->

<!-- BEGIN GENERATED:claim-c-guard-rejection -->
> **C-GUARD-REJECTION:** Guard rejection of an approved but unnecessary rollback: 2/2 refused, 0 Executor writes.
> Scope: One scenario: the service is healthy and already at its baseline. Counts Executor recovery writes only, after setup. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-guard-rejection -->

What is not claimed:

- Every live run uses one fault type, an invalid shipping `PORT`, where rolling
  back is always the right repair. The model's choice between different repairs
  is not tested.
- No committed live run shows an authenticated person approving. Runs before
  4139385 used pre-authorised lab approval. Runs from 4139385 create every
  approval through the authenticated approval API and, being unattended, opt
  in to automated lab approval, recorded as such; a person can instead type
  the action ID at the terminal, but no live run has done so (limitation
  `L-AGENT-APPROVAL`).
- Everything ran on one Windows/amd64 host with Docker Desktop. The Linux
  launcher has no recorded live run.
- Section 13 lists every known limitation and open defect.

## 2. Requirements

- Windows 11 on amd64, with Docker Desktop running its Linux engine (WSL 2).
  This is the only platform with live evidence.
- Git, to get the source.
- Network access for the first run: tools, Python packages, npm packages and
  container images are downloaded.
- Free loopback ports 8765 (Sentinel dashboard) and 8080 (Boutique storefront).
  The security lab uses 8081 and 8767 by default.

<!-- include: docs/setup.md#required-tools -->

The connected lab runs eleven Deployments, so give Docker Desktop enough memory
and CPU for all of their Pods to schedule. The project has not measured a
minimum. The first run downloads and imports images, which can take several
minutes. Agent mode with `qwen3:14b` keeps about 10-12 GB of GPU memory in use
while the model stays loaded.

## 3. Check the tools

On Windows, in PowerShell:

```powershell rehearse=P1 os=windows timeout=120 needs=docker
git --version
docker version
docker info --format '{{.OSType}}/{{.Architecture}}'
kind version
kubectl version --client
python --version
node --version
npm --version
```

On Linux:

```bash rehearse=P2 os=linux timeout=120 needs=docker
git --version
docker version
docker info --format '{{.OSType}}/{{.Architecture}}'
kind version
kubectl version --client
python3 --version
node --version
npm --version
```

The Docker server must respond and report `linux`. Python must be 3.12 or newer.

## 4. Get the source and set up (Windows)

The cover of the PDF gives the repository URL and the version. Set them for
this PowerShell session, for example `$env:SENTINEL_TAG = 'v1.0.0'`, and set
`$env:SENTINEL_REPO_URL` to the URL. The repository may be private; the team
grants markers access. If you received a source archive instead, extract it,
open PowerShell in its folder and skip step S1.

```powershell rehearse=S1 os=windows timeout=900 needs=network
git clone --branch $env:SENTINEL_TAG $env:SENTINEL_REPO_URL csc3104_sentinel
Set-Location csc3104_sentinel
```

Install missing tools. The installer keeps compatible tools and uses WinGet for
missing ones; review each installer prompt. Finish Docker Desktop's first-run
steps and any reboot yourself. It does not install Node.js, a model or
datasets; install Node.js separately if it is missing.

```powershell rehearse=S2 os=windows timeout=3600 needs=network,human
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1
```

Start Docker Desktop, then check. Expect the last line to read "TOOLS FOUND.
Docker Linux engine responds. No cluster was created or changed." Exit code 2
means a tool is missing; exit code 3 means the Docker Linux engine is not
running. The script may then print a "Next:" line naming
`scripts\sentinel-lab.bat`; that file no longer exists, so continue with step S4.

```powershell rehearse=S3 os=windows timeout=300 needs=docker
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1 -CheckOnly
```

`-ExecutionPolicy Bypass` applies to that one PowerShell process, not to your
system policy. Create the project's Python environment. The packages are exact,
hash-checked pins:

```powershell rehearse=S4 os=windows timeout=1200 needs=network
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Build the dashboard once (the launcher can also build it on first use):

```powershell rehearse=S5 os=windows timeout=1200 needs=network
Set-Location dashboard
npm ci
npm run build
Set-Location ..
```

## 5. Path A: the packaged runner

The packaged runner is a container image that runs the smaller shipping-only
walkthrough in its own kind cluster, `sentinel-boutique-package`. No release
image has been published yet (limitation `L-NO-RELEASE`), so `demo-start.ps1`
builds the image from your checkout first; that needs network access. The
committed packaged attempts so far:

<!-- BEGIN GENERATED:claim-c-packaged -->
> **C-PACKAGED:** The packaged runner image completed the Boutique walkthrough in 5/9 attempts.
> Scope: Attempts include image-build-only and interrupted starts. One fault type (invalid shipping PORT) where ROLLBACK is always the correct action; lab approval pre-authorised. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-packaged -->

Unattended: every scenario runs, the checks are evaluated and the cluster is
removed afterwards. Exit code 0 means the checks passed. The script prints the
evidence folder, under `data\sentinel\runs\packaged-boutique-...`.

```powershell rehearse=A1 os=windows timeout=3600 needs=network,docker,cluster
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -Unattended
```

`-Unattended` opts in to automated lab approval, recorded in `summary.json`
as `auto-lab`, never as a person.

From the browser instead: open http://127.0.0.1:8765, start the scenarios with
the dashboard buttons, type each action ID at the terminal when the Guard asks
for approval, then press Ctrl+C in the terminal when you have finished
looking and let the script remove the cluster.

```powershell rehearse=A2 os=windows timeout=5400 needs=network,docker,cluster,human
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-start.ps1 -UIControls
```

If a packaged run was interrupted, stop the runner and remove its cluster:

```powershell rehearse=A3 os=windows timeout=600 needs=docker optional=yes
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\demo-stop.ps1
```

## 6. Path B: the connected harness (main demonstration)

This is the main demonstration: all eleven Boutique Deployments in the
`sentinel-boutique-connected` cluster, with proposals from the fixed harness. It
needs no model and no cloud account.

```powershell rehearse=B1 os=windows timeout=5400 needs=network,docker,cluster,human
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup
```

Open Sentinel at http://127.0.0.1:8765/#overview and Boutique at
http://127.0.0.1:8080. Wait until the Overview page shows the healthy baseline
and the permission checks, then use the three buttons in order:

1. **Inject invalid PORT.** New shipping containers cannot start, so cart and
   checkout requests fail. The Observer detects the sustained failure, a
   harness proposal goes through the Guard, your approval, the restricted
   Executor and the Verifier. Expect APPLIED first and RECOVERED
   only after the Verifier has measured sustained health.
2. **Inject fault + crash Executor.** A real repair is applied, then the
   Executor process exits. Its restart reconciles from the cluster's state
   without a second repair write, and the Verifier measures recovery.
3. **Inject false recommendation.** The service is healthy and already matches
   its baseline. The Guard rejects the unnecessary rollback, with zero repair
   writes.

The buttons release fixed experiment steps; they do not approve actions or run
other Kubernetes commands. When the Guard asks for approval, the terminal that
started the run shows the action's facts and waits: type the exact action ID to
approve it; anything else refuses, and the run stops with no repair write. When
the terminal prints "Tests complete. Dashboard
stays live for inspection; press Ctrl+C to stop it.", look around, then press
Ctrl+C once and let the script remove the cluster.

The same walkthrough without a browser, with automatic cleanup. `-Approval auto-lab`
opts in to automated lab approval, recorded in `summary.json` under the
`automated-lab` identity, never as a person:

```powershell rehearse=B2 os=windows timeout=3600 needs=network,docker,cluster
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

A passing run ends with "PASS: two Boutique recoveries verified; final
unnecessary rollback REJECTED with zero Executor patch attempts" and exit code 0.

To watch the Pods while a run is in progress, use a second terminal:

```powershell rehearse=B3 os=windows timeout=600 needs=cluster,human optional=yes
kubectl --context kind-sentinel-boutique-connected -n sentinel-boutique-connected get pods -w
```

## 7. Path C: agent mode

Agent mode replaces the harness proposal with a local language model served by
Ollama. Install and start Ollama yourself, following its own instructions.
Sentinel never downloads a model. Check that the model is installed:

```powershell rehearse=C1 os=windows timeout=120 needs=model
ollama list
```

If `qwen3:14b` is not listed, download it (several gigabytes):

```powershell rehearse=C2 os=windows timeout=3600 needs=network,model optional=yes
ollama pull qwen3:14b
```

Before touching the cluster, the harness lists the installed models and sends
one small warm-up request, so the slow first load of the model happens before
the incident starts. If Ollama cannot be reached, the model is missing or the
warm-up fails, the run stops before any fault is injected and `summary.json`
records `agent_failure`. The model stays loaded for 30 minutes afterwards.

Run one agent case unattended, with automated lab approval:

```powershell rehearse=C3 os=windows timeout=3600 needs=network,docker,cluster,model
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Dashboard -ExitAfterTest -Approval auto-lab -Cleanup
```

Or from the browser, typing the action ID at the terminal when the Guard asks
for approval; agent mode runs the first fault and recovery only:

```powershell rehearse=C4 os=windows timeout=5400 needs=network,docker,cluster,model,human
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Agent -Dashboard -UIControls -Cleanup
```

A passing agent run ends with "PASS: live agent diagnosed the fault, proposed
rollback, and Coordinator verified RECOVERED". To use another installed model
or a longer timeout, save a JSON file such as the one below and add
`-AgentConfig <file>` to the command:

```json
{"base_url": "http://127.0.0.1:11434", "model": "your-installed-model", "timeout": 300}
```

## 8. Linux

The Linux launcher, `scripts/run-sentinel.sh`, is checked for syntax in CI only.
No live Linux run is recorded, so treat this path as not yet rehearsed. Check
the tools with step P2 first, and set `SENTINEL_REPO_URL` and `SENTINEL_TAG` as
in section 4.

```bash rehearse=L1 os=linux timeout=900 needs=network
git clone --branch "$SENTINEL_TAG" "$SENTINEL_REPO_URL" csc3104_sentinel
cd csc3104_sentinel
```

```bash rehearse=L2 os=linux timeout=1200 needs=network
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

```bash rehearse=L3 os=linux timeout=1200 needs=network
cd dashboard
npm ci
npm run build
cd ..
```

Unattended connected walkthrough with cleanup:

```bash rehearse=L4 os=linux timeout=3600 needs=network,docker,cluster
bash scripts/run-sentinel.sh --setup --cleanup --connected --dashboard --exit-after-test
```

The browser version (connected lab, dashboard and the three buttons), as in
section 6:

```bash rehearse=L5 os=linux timeout=5400 needs=network,docker,cluster,human
bash scripts/run-sentinel.sh --setup --cleanup
```

## 9. Optional: the security lab

The security lab is a separate experiment in its own cluster,
`sentinel-boutique-security`. It puts the shipping path behind an mTLS gateway
and probes it for bypasses. Shipping/cart inspection and protected shipping
recovery passed one Phase 5 smoke round on one Docker host; unscored, owner
sign-off pending ([results](../security.md)). OpenSSL
must be on PATH. Run each step on its own, and use a new `--directory` for each
attempt.

<!-- BEGIN GENERATED:claim-c-security-lab -->
> **C-SECURITY-LAB:** The protected Boutique security lab passed in 3/3 runs, blocking every bypass, identity and outage probe.
> Scope: Shipping path only, behind one mTLS gateway in a dedicated kind cluster; scripted probes, not an adversarial test. One Windows/amd64 host with Docker Desktop and kind; provenance inferred, not recorded.
<!-- END GENERATED:claim-c-security-lab -->

```powershell rehearse=D1 os=windows timeout=120
.\.venv\Scripts\python.exe scripts\security\boutique_security.py --help
```

```powershell rehearse=D2 os=windows timeout=3600 needs=network,docker,cluster
.\.venv\Scripts\python.exe scripts\security\boutique_security.py start --directory data\experimentation\runs\protected-boutique --exit-after-test
```

```powershell rehearse=D3 os=windows timeout=1800 needs=docker,cluster
.\.venv\Scripts\python.exe scripts\security\boutique_security.py test --directory data\experimentation\runs\protected-boutique
```

```powershell rehearse=D4 os=windows timeout=600 needs=docker,cluster
.\.venv\Scripts\python.exe scripts\security\boutique_security.py stop --directory data\experimentation\runs\protected-boutique
```

Without `--exit-after-test`, `start` keeps serving the storefront and dashboard
until stopped, and `test` would then fail because its ports are taken.

## 10. Reading the evidence

Each run writes a folder under `data\experimentation\runs\` (packaged runs:
`data\sentinel\runs\`) and prints its path. `summary.json` in that folder holds
the checks that passed or failed, the source revision and tool versions
(`provenance`) and a phase timeline. To show the newest run's summary:

```powershell rehearse=E1 os=windows timeout=60
$run = Get-ChildItem data\experimentation\runs -Directory | Sort-Object LastWriteTime | Select-Object -Last 1
Get-Content (Join-Path $run.FullName 'summary.json')
```

The project's committed results are in `docs/results.md`, generated from the
sanitised bundles under `evidence/`. This check confirms that every bundle still
matches its manifest and that the tables and claim values are current:

```powershell rehearse=E2 os=windows timeout=300
.\.venv\Scripts\python.exe scripts\evidence\summarize.py --check
```

Compare your `summary.json` with the bundles of the same class in
`evidence/`. To keep your run as evidence, collect it into a sanitised bundle
as described in `evidence/README.md`.

## 11. Troubleshooting

Common problems and what to do about them.

- **Docker Desktop does not start.** Step S3 exits with code 3. Start Docker
  Desktop, choose Linux containers and run S3 again. If the engine still does
  not respond, check that WSL 2 and hardware virtualisation are enabled.
- **A port is taken.** Find what is listening, then rerun with other ports:

```powershell rehearse=T1 os=windows timeout=60 optional=yes
Get-NetTCPConnection -LocalPort 8765,8080 -State Listen
```

```powershell rehearse=T2 os=windows timeout=5400 needs=network,docker,cluster,human optional=yes
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-boutique-live.ps1 -Setup -Connected -Dashboard -UIControls -Cleanup -StorefrontPort 8081 -DashboardPort 8766
```

- **"Run with -Setup to create the dedicated Boutique lab."** (Windows) or
  **"Use --setup to create ..."** (Linux). The lab cluster does not exist yet;
  add `-Setup` or `--setup`.
- **The security lab cannot open an `openssl.cfg`.** A stale `OPENSSL_CONF`
  environment variable is the usual cause. Clear it for this session:

```powershell rehearse=T3 os=windows timeout=60 optional=yes
Remove-Item Env:OPENSSL_CONF
```

- **Agent mode is slow to start or times out.** The first load of a large model
  can take minutes; the longest recorded agent run waited for the 300-second
  client timeout. The harness warms the model before the incident, and a failed
  warm-up stops the run before any fault is injected. Raise `timeout` or
  `warm_up_timeout_seconds` in an `-AgentConfig` file, or use a smaller model.
- **Garbled text or an image-import failure on a UTF-8 console.** Older
  revisions of the packaged runner failed on code page 65001. This is fixed
  (`L-PACKAGED-UTF8`); use the release revision.

## 12. Cleanup

With `-Cleanup` or `--cleanup`, Ctrl+C or the end of an unattended run removes
the lab cluster after stopping the dashboard and port forwarding and deleting
the private Executor credentials. Let it finish rather than closing the
terminal. The scripts create and delete only their own clusters:
`sentinel-boutique-connected` (Path B and C), `sentinel-boutique-lab` (the
smaller lab without `-Connected`), `sentinel-boutique-package` (Path A) and
`sentinel-boutique-security` (section 9). They keep your current kubectl
context, never prune Docker and leave evidence and cached images in place.
Do not delete clusters these scripts did not create.

List the clusters that remain:

```powershell rehearse=X1 os=windows timeout=60 needs=docker
kind get clusters
```

If an interrupted run left the connected lab behind:

```powershell rehearse=X2 os=windows timeout=600 needs=docker optional=yes
kind delete cluster --name sentinel-boutique-connected
kind get clusters
```

On Linux:

```bash rehearse=X3 os=linux timeout=600 needs=docker optional=yes
kind delete cluster --name sentinel-boutique-connected
kind get clusters
```

For an interrupted packaged run use step A3; for the security lab use step D4.

## 13. Known limitations and open defects

<!-- include: README.md#known-limitations-and-open-defects -->

## 14. Repository, release and access

The repository URL and version are on the cover. No tagged release or published
runner image exists yet (`L-NO-RELEASE`); until one does, run from the source
tag on the cover. If the repository is private, the team grants markers access
or supplies a source archive with the submission.
