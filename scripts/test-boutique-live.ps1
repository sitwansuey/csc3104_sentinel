[CmdletBinding()]
param([switch]$Setup, [switch]$Cleanup, [switch]$Dashboard,
      [ValidateRange(0,65535)][int]$DashboardPort = 8765, [switch]$ExitAfterTest,
      [switch]$PauseBeforeInjection, [switch]$GuardRejection, [switch]$UIControls, [switch]$Connected,
      [ValidateRange(1,65535)][int]$StorefrontPort = 8080,
      [switch]$Agent, [string]$AgentConfig)

$ErrorActionPreference = 'Stop'
if ($ExitAfterTest -and -not $Dashboard) { throw '-ExitAfterTest requires -Dashboard.' }
if ($AgentConfig -and -not $Agent) { throw '-AgentConfig requires -Agent.' }
if ($Agent -and $GuardRejection) { throw '-Agent and -GuardRejection are separate scenarios.' }
if ($UIControls -and (-not $Dashboard -or $PauseBeforeInjection)) { throw '-UIControls requires -Dashboard and replaces -PauseBeforeInjection.' }
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

$clusterName = if ($Connected) { 'sentinel-boutique-connected' } else { 'sentinel-boutique-lab' }
$resultCode = 1
try {
    $clusters = @(& $kindTool.Path get clusters)
    if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters.' }
    if ($clusters -notcontains $clusterName) {
        if (-not $Setup) { throw 'Run with -Setup to create the dedicated Boutique lab.' }
        & $kindTool.Path create cluster --name $clusterName --config (Join-Path $projectRoot 'deploy/kind/kind.yaml') --wait 120s
        if ($LASTEXITCODE -ne 0) { throw 'Boutique cluster setup failed.' }
    }
    $pythonPrefix = @()
    if ((Split-Path -Leaf $pythonTool.Path) -eq 'py.exe') { $pythonPrefix = @('-3') }
    $testOptions = @()
    if ($Agent) { $testOptions += '--agent' }
    if ($AgentConfig) { $testOptions += @('--agent-config', $AgentConfig) }
    if ($Dashboard) { $testOptions += @('--dashboard', '--dashboard-port', "$DashboardPort") }
    if ($ExitAfterTest) { $testOptions += '--exit-after-test' }
    if ($PauseBeforeInjection) { $testOptions += '--pause-before-injection' }
    if ($UIControls) { $testOptions += '--ui-controls' }
    if ($GuardRejection) { $testOptions += '--guard-rejection' }
    if ($Connected) { $testOptions += @('--connected','--storefront-port',"$StorefrontPort") }
    & $pythonTool.Path @pythonPrefix (Join-Path $PSScriptRoot 'test_boutique_live.py') @testOptions
    $resultCode = $LASTEXITCODE
} finally {
    if ($Cleanup) {
        & $kindTool.Path delete cluster --name $clusterName
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove the Boutique cluster.' }
        $remaining = @(& $kindTool.Path get clusters)
        if ($LASTEXITCODE -ne 0 -or $remaining -contains $clusterName) { throw 'Boutique cleanup could not be verified.' }
        Write-Host "Removed $clusterName. Evidence and cached images are retained."
    }
}
exit $resultCode
