[CmdletBinding()]
param([switch]$Setup, [switch]$Cleanup)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
# Reuse discovery, without installing software or changing persistent settings.
. (Join-Path $PSScriptRoot 'install-dependencies.ps1') -FunctionsOnly
Refresh-ToolPaths
$dependencies = @(Get-Dependencies)
Show-Dependencies $dependencies
if (@($dependencies | Where-Object { -not $_.Tool }).Count) {
    throw 'Run scripts\install-dependencies.ps1 first; required tools are unavailable.'
}
$pythonTool = ($dependencies | Where-Object { $_.Id -eq 'Python.Python.3.14' }).Tool
$dockerTool = ($dependencies | Where-Object { $_.Id -eq 'Docker.DockerDesktop' }).Tool
$kindTool = ($dependencies | Where-Object { $_.Id -eq 'Kubernetes.kind' }).Tool
$engine = Invoke-Probe $dockerTool.Path 'info --format "{{.OSType}}"'
if (-not $engine -or $engine.Output -ne 'linux') { throw 'Start Docker Desktop with Linux containers first.' }

$resultCode = 1
try {
    if ($Setup) {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'sentinel-lab.ps1') -Action Setup
        if ($LASTEXITCODE -ne 0) { throw 'Local kind lab setup failed.' }
    }
    $pythonPrefix = @()
    if ((Split-Path -Leaf $pythonTool.Path) -eq 'py.exe') { $pythonPrefix = @('-3') }
    & $pythonTool.Path @pythonPrefix (Join-Path $PSScriptRoot 'test_executor_live.py')
    $resultCode = $LASTEXITCODE
} finally {
    if ($Cleanup) {
        # User-selected teardown is scoped to this repository's named local lab.
        & $kindTool.Path delete cluster --name sentinel-lab
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove the sentinel-lab cluster; inspect it manually.' }
        $remaining = @(& $kindTool.Path get clusters)
        if ($LASTEXITCODE -ne 0 -or $remaining -contains 'sentinel-lab') {
            throw 'Cluster teardown could not be verified.'
        }
        Write-Host 'Removed sentinel-lab. Evidence in runs/ and cached images are retained.'
    }
}
exit $resultCode
