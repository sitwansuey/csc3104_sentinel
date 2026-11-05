# Install and prepare

Run commands from the repository root. The current live-validated host is
Windows/amd64 with Docker Desktop's Linux engine. kind creates the workload
cluster; Docker Desktop's separate built-in Kubernetes cluster is unnecessary.
Full Boutique runs eleven Deployments, so allow enough Docker memory/CPU for
all Pods to schedule. Initial image downloads/imports can take several minutes.

## Required tools

| Tool | Purpose |
| --- | --- |
| Docker Desktop / Linux Docker engine | kind node and workload/container images |
| kind | Dedicated local Kubernetes cluster |
| kubectl | Explicit-context collection, probes and restricted repair |
| Python 3.12+ | Sentinel workers, harness and tests |
| Node.js/npm compatible with `dashboard/package.json` | First dashboard build; CI uses Node 22 |

Ollama is required only for real agent mode. Helm is required only for Chaos
Mesh. RCAEval data and its scientific packages are optional benchmark inputs;
they are not required to run the connected harness demo.

## Method 1: use the Windows installer

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1
```

The installer keeps compatible tools and uses WinGet for missing core tools.
`scripts/install-dependencies.bat` is its double-click entrypoint. Complete
Docker Desktop/WSL first-run prerequisites and any requested reboot yourself.
It does not silently enable Windows features, install a model or fetch datasets.
Install Node/npm separately if absent. Start Docker Desktop, then check:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-dependencies.ps1 -CheckOnly
```

`-ExecutionPolicy Bypass` affects only that PowerShell process, not global policy.

## Method 2: install manually

Install [Docker Desktop](https://docs.docker.com/desktop/),
[kind](https://kind.sigs.k8s.io/docs/user/quick-start/),
[kubectl](https://kubernetes.io/docs/tasks/tools/),
[Python](https://www.python.org/downloads/) and [Node.js](https://nodejs.org/en/download).
Use binaries matching the host CPU, add their directories to your user PATH,
and reopen the terminal. On macOS/Linux, use their documented installation or
your package manager. Check:

```text
docker version
docker info --format '{{.OSType}}/{{.Architecture}}'
kind version
kubectl version --client
python --version
node --version
npm --version
```

The Docker server must respond and report Linux. Use `python3` on hosts where
that is the installed interpreter name. The shell launcher accepts
`SENTINEL_PYTHON=/path/to/python` and prefers `.venv/bin/python` when present.

The connected workload/import path is currently tested on amd64. The earlier
Apple Silicon single-service packaging support is not proof that the complete
connected/agent/Chaos stack works on arm64. Check image/node architectures and
rehearse on that machine; no new macOS acceptance is claimed here.

## Project Python environment and frontend

Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Linux/macOS:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

The core requirements supply the RCA CSV reader. The harness and restricted
execution core use the standard library. Windows launchers prefer the project
`.venv` so installed agent dependencies are not lost by selecting another Python.
No global Python package install is required.

```text
cd dashboard
npm ci
npm run build
```

Return to the repository root afterwards. The dashboard launcher can build on
first use as well. Do not install benchmark packages/model data just to launch
the harness. See [testing](testing.md) for full test dependencies and
[Planner mode](planner.md) for the separate model setup.

Continue with [run and observe](run.md).
