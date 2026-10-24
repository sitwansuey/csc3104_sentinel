# Local development dependencies

Start with the [README setup walkthrough](../README.md#start-here-local-setup-on-windows)
for the complete install, readiness check, test and cleanup sequence. This guide
provides installation details and troubleshooting.

Sentinel's Python checks and executor use the standard library. The local live
lab also needs Docker with Linux containers, kind and kubectl. Installing these
tools does not deploy Sentinel or prove that recovery works.

## Windows

There are two supported setup methods. Both lead to the same real Kubernetes
experiment and use the same application/Executor code.

| Method | How tools and the environment are prepared |
| --- | --- |
| 1. Scripted | The dependency script installs missing tools; the live-test wrapper creates the cluster, deploys the app, tests and optionally cleans up |
| 2. Manual | You download/install the programs, configure PATH and Docker, and run the explicit kind/Docker/kubectl setup commands in the README |

### Method 1: installation and setup scripts

From the repository root, inspect the current machine without installing:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\install-dependencies.ps1 -CheckOnly
```

To install missing tools, run the same command without `-CheckOnly`, or
double-click `scripts\install-dependencies.bat`. Existing usable tools are kept;
the script does not upgrade them. It uses these exact WinGet package IDs:

| Tool | Package selected when missing |
| --- | --- |
| Docker Desktop | `Docker.DockerDesktop` |
| kind | `Kubernetes.kind` |
| kubectl | `Kubernetes.kubectl` |
| Python 3.12 or newer | `Python.Python.3.14` |

A working newer Python, including Python Install Manager runtimes, satisfies the
requirement. An older Python can remain installed alongside the selected runtime.
The script checks actual executable versions and looks in common per-user Docker,
WinGet portable-package and Python directories. An inaccessible WindowsApps alias
does not count as a working Python installation. Custom installation locations
should be on PATH. It refreshes PATH only in its own process; installers may add
their normal PATH entries. Reopen a terminal after installation if needed.
Version probes disable Python launchers' automatic runtime installation. The
[Python Windows guide](https://docs.python.org/3/using/windows.html#configuration)
documents this Python Install Manager setting.

For this method, WinGet must already be available through Microsoft's App Installer. The
installer uses exact IDs from the `winget` source and interactive installers.
Review package/source terms and any elevation request yourself. It passes no
automatic agreement-acceptance, hash-bypass or reboot flags. Docker's installer
arguments explicitly omit license acceptance and request per-user WSL 2 mode;
Windows feature dependencies are left for the user to configure. The script does
not start Docker Desktop, enable WSL/virtualization, request a reboot, create a
cluster or change Kubernetes contexts.

Docker Desktop's Windows prerequisites include a supported Windows release, WSL 2
and enabled hardware virtualization. See the [Docker installation guide](https://docs.docker.com/desktop/setup/install/windows-install/)
for current requirements and manual setup. Start Docker Desktop yourself after
installation, review its terms and select Linux containers. Docker Desktop's
built-in Kubernetes need not be enabled: Sentinel's lab creates a separate kind
cluster using Docker.

The checker reports the installed tools separately from Docker engine readiness.
It only probes client versions and `docker info`; it makes no cluster API calls.

| Exit code | Meaning |
| --- | --- |
| 0 | Required tools were found and the Docker Linux engine responds; no cluster readiness claim |
| 1 | Installation or checker error; inspect the displayed failure |
| 2 | Required tools are missing or unusable |
| 3 | Tools were found, but the Docker Linux engine is stopped, inaccessible or using Windows containers |

Run `scripts\sentinel-lab.bat` when ready. Its menu has separate setup, tests,
live experiments and cleanup actions. Docker images may still need downloading
at setup time. Cluster creation, RBAC and live action verification are separate
from dependency installation.

Package choices are documented by [kind's installation guide](https://kind.sigs.k8s.io/docs/user/quick-start/),
[Kubernetes' kubectl Windows guide](https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/),
[Microsoft's Python setup guide](https://learn.microsoft.com/en-us/windows/dev-environment/python)
and the [Microsoft WinGet Docker package manifests](https://github.com/microsoft/winget-pkgs/tree/master/manifests/d/Docker/DockerDesktop).
The [WinGet install reference](https://learn.microsoft.com/en-us/windows/package-manager/winget/install)
defines interactive, exact-ID, scope and override options. These are development
dependencies, not pinned evaluation versions; capture actual tool/image versions
with each experiment.

### Method 2: manual installation and environment setup

The manual route does not require WinGet or the dependency installer:

1. Install [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/),
   complete its supported Windows/WSL 2 and virtualization prerequisites, start
   it and select Linux containers. Its built-in Kubernetes cluster is not needed.
2. Install [Python 3.12+](https://www.python.org/downloads/windows/) from an
   official installer or Install Manager. Verify the actual interpreter version;
   an alias pointing to the Microsoft Store is not an installed runtime.
3. Download the Windows [kind binary](https://kind.sigs.k8s.io/docs/user/quick-start/)
   for your architecture and rename it `kind.exe`.
4. Download [kubectl for Windows](https://kubernetes.io/docs/tasks/tools/install-kubectl-windows/)
   for your architecture, follow its checksum-verification instructions and
   select a version compatible with the Kubernetes node version used by kind.
5. Put `kind.exe` and `kubectl.exe` in a tools directory you control. Add that
   directory to your user **Path** using Windows' **Edit environment variables
   for your account** dialog. Make Docker's CLI and Python available on PATH too,
   and reopen PowerShell so it receives the updated environment.

Verify `docker version`, `docker info --format '{{.OSType}}'`, `kind version`,
`kubectl version --client` and `python --version`. Docker must have a reachable
Linux server, and Python must be 3.12 or newer. No Python packages are required
for the provided local experiment.

Then follow [README Method 2](../README.md#method-2-install-and-set-up-manually)
to create `sentinel-lab`, build/load the image, apply `infra/demo.yaml`, wait for
the healthy Deployment and run `python scripts/test_executor_live.py`. That
Python command runs the actual Executor experiment against the manually prepared
cluster. It is not the old operator rollback demo. Delete the cluster afterward
with `kind delete cluster --name sentinel-lab` and retain the generated evidence.

## Linux and macOS

Use Python 3.12+, Docker Engine or Docker Desktop with a running Linux engine,
kind, kubectl and Bash. No cross-platform installer is provided because engine
installation and privileges vary by distribution. Follow the upstream
[Docker Engine guide](https://docs.docker.com/engine/install/),
[kind guide](https://kind.sigs.k8s.io/docs/user/quick-start/) and
[kubectl installation guide](https://kubernetes.io/docs/tasks/tools/).
Ensure your user can access the Docker engine without making the lab scripts
run as root. Keep kubectl within the Kubernetes-supported version skew of the
cluster; the installer does not check that compatibility or pin a kind node image.

Check tools before running the existing Bash lab:

```bash
python3 --version
docker version
docker info --format '{{.OSType}}'
kind version
kubectl version --client
bash -n scripts/lab.sh
```

Use `python3 scripts/check_contracts.py` and the discovered Python tests for
offline checks. The Bash lab's recovery action is an operator baseline; consult
the Executor guide for the guarded execution runtime and its current live scope.
