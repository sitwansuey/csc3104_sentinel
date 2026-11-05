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
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $projectPython -PathType Leaf) {
    $pythonTool = [pscustomobject]@{Path = $projectPython}
    Write-Host "Using project Python: $projectPython"
}
$dockerTool = ($dependencies | Where-Object { $_.Id -eq 'Docker.DockerDesktop' }).Tool
$kindTool = ($dependencies | Where-Object { $_.Id -eq 'Kubernetes.kind' }).Tool
$kubectlTool = ($dependencies | Where-Object { $_.Id -eq 'Kubernetes.kubectl' }).Tool
$engine = Invoke-Probe $dockerTool.Path 'info --format "{{.OSType}}"'
if (-not $engine -or $engine.Output -ne 'linux') { throw 'Start Docker Desktop with Linux containers first.' }

$resultCode = 1
try {
    if ($Setup) {
        $clusters = @(& $kindTool.Path get clusters)
        if ($LASTEXITCODE -ne 0) { throw 'Could not inspect kind clusters.' }
        if ($clusters -notcontains 'sentinel-lab') {
            & $kindTool.Path create cluster --name sentinel-lab --config (Join-Path $projectRoot 'deploy/kind/kind.yaml') --wait 120s
            if ($LASTEXITCODE -ne 0) { throw 'Local kind lab setup failed.' }
        }
        & $dockerTool.Path build -t sentinel-demo:0.1.0 (Join-Path $projectRoot 'experiments/workloads/demo')
        if ($LASTEXITCODE -ne 0) { throw 'Could not build the rollback fixture.' }
        & $kindTool.Path load docker-image sentinel-demo:0.1.0 --name sentinel-lab
        if ($LASTEXITCODE -ne 0) { throw 'Could not load the rollback fixture.' }
        & $kubectlTool.Path --context kind-sentinel-lab apply -f (Join-Path $projectRoot 'deploy/workloads/demo.yaml')
        if ($LASTEXITCODE -ne 0) { throw 'Could not deploy the rollback fixture.' }
        & $kubectlTool.Path --context kind-sentinel-lab -n sentinel-demo rollout status deployment/demo-api --timeout=120s
        if ($LASTEXITCODE -ne 0) { throw 'Rollback fixture did not become ready.' }
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
        Write-Host 'Removed sentinel-lab. Evidence in data/sentinel/runs/ and cached images are retained.'
    }
}
exit $resultCode
