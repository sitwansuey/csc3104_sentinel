[CmdletBinding()]
param([switch]$Setup, [switch]$Cleanup, [switch]$Dashboard,
      [ValidateRange(0,65535)][int]$DashboardPort = 8765, [switch]$ExitAfterTest,
      [switch]$PauseBeforeInjection, [string]$ProviderScript, [string]$ProviderPolicy,
      [string]$Operator, [string]$RunDirectory)

$ErrorActionPreference = 'Stop'
if ($ExitAfterTest -and -not $Dashboard) { throw '-ExitAfterTest requires -Dashboard.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
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

$clusterName = 'sentinel-boutique-lab'
$resultCode = 1
try {
    $clusters = @(& $kindTool.Path get clusters)
    if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters.' }
    if ($clusters -notcontains $clusterName) {
        if (-not $Setup) { throw 'Run with -Setup to create the dedicated Boutique lab.' }
        & $kindTool.Path create cluster --name $clusterName --config (Join-Path $projectRoot 'infra/kind.yaml') --wait 120s
        if ($LASTEXITCODE -ne 0) { throw 'Boutique cluster setup failed.' }
    }
    $pythonPrefix = @()
    if ((Split-Path -Leaf $pythonTool.Path) -eq 'py.exe') { $pythonPrefix = @('-3') }
    $testOptions = @()
    if ($ProviderScript) { $testOptions += @('--provider-script', $ProviderScript) }
    if ($ProviderPolicy) { $testOptions += @('--provider-policy', $ProviderPolicy) }
    if ($Operator) { $testOptions += @('--operator', $Operator) }
    if ($RunDirectory) { $testOptions += @('--run-dir', $RunDirectory) }
    if ($Dashboard) { $testOptions += @('--dashboard', '--dashboard-port', "$DashboardPort") }
    if ($ExitAfterTest) { $testOptions += '--exit-after-test' }
    if ($PauseBeforeInjection) { $testOptions += '--pause-before-injection' }
    & $pythonTool.Path @pythonPrefix (Join-Path $PSScriptRoot 'test_boutique_live.py') @testOptions
    $resultCode = $LASTEXITCODE
} finally {
    if ($Cleanup) {
        & $kindTool.Path delete cluster --name $clusterName
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove the Boutique cluster.' }
        $remaining = @(& $kindTool.Path get clusters)
        if ($LASTEXITCODE -ne 0 -or $remaining -contains $clusterName) { throw 'Boutique cleanup could not be verified.' }
        Write-Host 'Removed sentinel-boutique-lab. Evidence and cached images are retained.'
    }
}
exit $resultCode
