[CmdletBinding()]
param([switch]$CheckOnly, [switch]$FunctionsOnly)

$ErrorActionPreference = 'Stop'

function Add-ProcessPath([string[]]$Directories) {
    $entries = @($Directories) + @($env:Path -split ';')
    $env:Path = ($entries | Where-Object { $_ } | Select-Object -Unique) -join ';'
}

function Refresh-ToolPaths {
    # Changes this process only. Package installers may manage their own PATH entries.
    Add-ProcessPath @(
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    )
    $directories = @(
        "$env:LOCALAPPDATA\Programs\DockerDesktop\resources\bin",
        "$env:ProgramFiles\Docker\Docker\resources\bin",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Python\bin"
    )
    foreach ($root in @("$env:LOCALAPPDATA\Python", "$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles)) {
        if (Test-Path -LiteralPath $root) {
            $directories += Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -like 'pythoncore-*' -or $_.Name -match '^Python\d+$' } |
                Select-Object -ExpandProperty FullName
        }
    }
    foreach ($root in @("$env:LOCALAPPDATA\Microsoft\WinGet\Packages", "$env:ProgramFiles\WinGet\Packages")) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $packages = Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -like 'Kubernetes.kind_*' -or $_.Name -like 'Kubernetes.kubectl_*' }
        foreach ($package in $packages) {
            $directories += Get-ChildItem -LiteralPath $package.FullName -Filter '*.exe' -File -Recurse |
                Select-Object -ExpandProperty DirectoryName
        }
    }
    # Prefer an explicitly installed kubectl over Docker Desktop's bundled copy.
    Add-ProcessPath @($directories | Where-Object { Test-Path -LiteralPath $_ -PathType Container } |
        Sort-Object { if ($_ -like '*\WinGet\Packages\Kubernetes.*') { 0 } else { 1 } })
}

function Invoke-Probe([string]$Executable, [string]$Arguments) {
    # Bounded read-only probes; a stopped Docker engine must not hang the checker.
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $Executable
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    # Version discovery must not ask Python launchers to install a missing runtime.
    $process.StartInfo.EnvironmentVariables['PYTHON_MANAGER_AUTOMATIC_INSTALL'] = 'false'
    $process.StartInfo.EnvironmentVariables.Remove('PYLAUNCHER_ALLOW_INSTALL')
    $process.StartInfo.EnvironmentVariables.Remove('PYLAUNCHER_ALWAYS_INSTALL')
    try {
        if (-not $process.Start()) { return $null }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill()
            $process.WaitForExit()
            return $null
        }
        if ($process.ExitCode -ne 0) { return $null }
        return [pscustomobject]@{ Path = $Executable; Output = $stdout.Result.Trim() }
    } catch {
        # Inaccessible WindowsApps aliases are candidates, not proof of a runtime.
        return $null
    } finally {
        $process.Dispose()
    }
}

function Find-Program([string]$Name, [string]$Arguments) {
    foreach ($command in @(Get-Command $Name -All -CommandType Application -ErrorAction SilentlyContinue)) {
        $probe = Invoke-Probe $command.Source $Arguments
        if ($probe) { return $probe }
    }
    return $null
}

function Find-PythonRuntime {
    $check = '-c "import sys; print(sys.version.split()[0]); sys.exit(0 if sys.version_info >= (3,12) else 1)"'
    foreach ($name in @('python.exe', 'python3.exe', 'py.exe')) {
        $arguments = if ($name -eq 'py.exe') { '-3 ' + $check } else { $check }
        $runtime = Find-Program $name $arguments
        if ($runtime) { return $runtime }
    }
    return $null
}

function Find-DesktopInstallation {
    $paths = @(
        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe",
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    )
    foreach ($root in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
                         'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall')) {
        if (-not (Test-Path $root)) { continue }
        foreach ($entry in @(Get-ItemProperty "$root\*" -ErrorAction SilentlyContinue)) {
            if ($entry.DisplayName -eq 'Docker Desktop' -and $entry.InstallLocation) {
                $paths += Join-Path $entry.InstallLocation 'Docker Desktop.exe'
            }
        }
    }
    return $paths | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}

function Get-Dependencies {
    $docker = Find-Program 'docker.exe' '--version'
    $desktop = Find-DesktopInstallation
    if ($desktop -and -not $docker) {
        Add-ProcessPath @((Join-Path (Split-Path -Parent $desktop) 'resources\bin'))
        $docker = Find-Program 'docker.exe' '--version'
    }
    @(
        [pscustomobject]@{ Name = $(if ($desktop) { 'Docker Desktop' } else { 'Docker CLI (Desktop location unconfirmed)' }); Id = 'Docker.DockerDesktop'; Tool = $docker; Installed = [bool]$desktop },
        [pscustomobject]@{ Name = 'kind'; Id = 'Kubernetes.kind'; Tool = (Find-Program 'kind.exe' 'version'); Installed = $false },
        [pscustomobject]@{ Name = 'kubectl'; Id = 'Kubernetes.kubectl'; Tool = (Find-Program 'kubectl.exe' 'version --client'); Installed = $false },
        [pscustomobject]@{ Name = 'Python >= 3.12'; Id = 'Python.Python.3.14'; Tool = (Find-PythonRuntime); Installed = $false }
    )
}

function Show-Dependencies($Dependencies) {
    foreach ($dependency in $Dependencies) {
        if ($dependency.Tool) {
            $version = ($dependency.Tool.Output -split "`r?`n")[0]
            Write-Host ("FOUND {0}: {1}" -f $dependency.Name, $version)
            Write-Host ('      ' + $dependency.Tool.Path)
        } elseif ($dependency.Installed) {
            Write-Host ("REPAIR {0}: installed application found, but its CLI is unavailable." -f $dependency.Name)
        } else {
            Write-Host ('MISSING ' + $dependency.Name)
        }
    }
}

if ($FunctionsOnly) { return }

try {
    Refresh-ToolPaths
    $dependencies = @(Get-Dependencies)
    Show-Dependencies $dependencies
    $missing = @($dependencies | Where-Object { -not $_.Tool -and -not $_.Installed })
    if ($missing.Count -and -not $CheckOnly) {
        $winget = Find-Program 'winget.exe' '--version'
        if (-not $winget) {
            throw 'WinGet is unavailable. Install Microsoft App Installer, then rerun; see docs/setup/dependencies.md.'
        }
        foreach ($dependency in $missing) {
            Write-Host ('Installing missing package ' + $dependency.Id + '; review any installer prompts.')
            $arguments = @('install', '--exact', '--id', $dependency.Id,
                           '--source', 'winget', '--no-upgrade', '--interactive')
            if ($dependency.Id -eq 'Docker.DockerDesktop') {
                # Override package defaults: do not accept Docker terms on the user's behalf.
                $arguments += @('--skip-dependencies', '--override', 'install --user --backend=wsl-2')
            } elseif ($dependency.Id -eq 'Python.Python.3.14') {
                $arguments += @('--scope', 'user')
            }
            & $winget.Path @arguments
            if ($LASTEXITCODE -ne 0) { throw "WinGet failed for $($dependency.Id) (exit $LASTEXITCODE)." }
            Refresh-ToolPaths
        }
        $dependencies = @(Get-Dependencies)
        Show-Dependencies $dependencies
    }
    if (@($dependencies | Where-Object { -not $_.Tool }).Count) {
        Write-Host 'Dependencies remain missing or unusable. Install/repair them and rerun -CheckOnly.'
        Write-Host 'Docker Desktop needs a supported Windows release, WSL 2 and hardware virtualization.'
        Write-Host 'This script does not enable Windows features, accept licenses, or reboot the machine.'
        exit 2
    }

    $docker = ($dependencies | Where-Object { $_.Id -eq 'Docker.DockerDesktop' }).Tool
    $engine = Invoke-Probe $docker.Path 'info --format "{{.OSType}}"'
    if (-not $engine -or $engine.Output -ne 'linux') {
        Write-Host 'TOOLS FOUND. Docker Linux engine is unavailable; the Kubernetes lab is not ready.'
        Write-Host 'Start Docker Desktop yourself, review its terms, select Linux containers, and rerun -CheckOnly.'
        Write-Host 'If it cannot start, check WSL 2 and BIOS/UEFI virtualization; see docs/setup/dependencies.md.'
        exit 3
    }
    Write-Host 'TOOLS FOUND. Docker Linux engine responds. No cluster was created or changed.'
    Write-Host 'Next: scripts\executor\sentinel-lab.bat (the lab uses kind, not Docker Desktop built-in Kubernetes).'
    exit 0
} catch {
    Write-Error $_.Exception.Message -ErrorAction Continue
    exit 1
}
