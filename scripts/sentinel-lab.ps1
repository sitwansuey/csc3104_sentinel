[CmdletBinding()]
param(
    [ValidateSet('Menu', 'Setup', 'Test', 'LiveTest', 'Status', 'Recover', 'Cleanup', 'Full', 'Guard', 'Dashboard')]
    [string]$Action = 'Menu'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
# Pick up newly installed WinGet tools even when the launching terminal is older.
$env:Path += ';' + [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
$script:Context = 'kind-sentinel-lab'
$script:Namespace = 'sentinel-demo'
$script:Cluster = 'sentinel-lab'
$script:Image = 'sentinel-demo:0.1.0'
$script:PythonExe = $null
$script:PythonPrefix = @()
$runName = 'windows-lab-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$script:RunDirectory = Join-Path $projectRoot ('runs\' + $runName)
New-Item -ItemType Directory -Force -Path $script:RunDirectory | Out-Null
$script:BaselineFile = Join-Path $projectRoot 'runs\windows-lab-baseline.json'
$script:CoordinatorDb = 'runs/coordinator.sqlite3'
$script:TelemetryDb = 'runs/telemetry.sqlite3'
$script:DashboardWindows = @()

function Require-Tool([string]$Name) {
    $command = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $command) {
        throw "$Name is not on PATH. Install the tool, then reopen this script."
    }
    return $command.Source
}

function Invoke-Tool([string]$Name, [string[]]$Arguments) {
    $executable = Require-Tool $Name
    Write-Host ('> ' + $Name + ' ' + ($Arguments -join ' ')) -ForegroundColor Cyan
    & $executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE." }
}

function Invoke-Kube([string[]]$Arguments) {
    Invoke-Tool 'kubectl' (@('--context', $script:Context, '--namespace', $script:Namespace) + $Arguments)
}

function Require-Docker {
    Invoke-Tool 'docker' @('info', '--format', '{{.OSType}}')
    # The command failing is enough to stop; the image build will also reject an incompatible engine.
}

function Find-Python {
    if ($script:PythonExe) { return }
    foreach ($name in @('py', 'python', 'python3')) {
        $command = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $command) { continue }
        $prefix = @()
        if ($name -eq 'py') { $prefix = @('-3') }
        & $command.Source @prefix -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'
        if ($LASTEXITCODE -eq 0) {
            $script:PythonExe = $command.Source
            $script:PythonPrefix = $prefix
            return
        }
    }
    throw 'Python 3.12 or newer is required. Install Python and reopen this script.'
}

function Invoke-Python([string[]]$Arguments) {
    Find-Python
    Write-Host ('> Python ' + ($Arguments -join ' ')) -ForegroundColor Cyan
    & $script:PythonExe @script:PythonPrefix @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Python failed with exit code $LASTEXITCODE." }
}

function Start-PythonWindow([string]$Title, [string[]]$Arguments) {
    Find-Python
    # An encoded command avoids re-quoting paths with spaces; -NoExit keeps errors visible.
    $quote = { param($Text) "'" + ($Text -replace "'", "''") + "'" }
    $quoted = @(@($script:PythonExe) + $script:PythonPrefix + $Arguments | ForEach-Object { & $quote $_ })
    $command = "`$Host.UI.RawUI.WindowTitle = $(& $quote $Title); Set-Location -LiteralPath $(& $quote $projectRoot); & $($quoted -join ' ')"
    $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    Write-Host ('> Python ' + ($Arguments -join ' ') + "  [new window: $Title]") -ForegroundColor Cyan
    return Start-Process powershell.exe -PassThru -ArgumentList @('-NoLogo', '-NoProfile', '-NoExit', '-EncodedCommand', $encoded)
}

function Start-LiveDashboard {
    Require-Tool 'kubectl' | Out-Null
    if (@($script:DashboardWindows | Where-Object { -not $_.HasExited }).Count -gt 0) {
        Write-Host 'Dashboard windows from this menu are still open. Close them to start fresh ones.'
        return
    }
    $server = Start-PythonWindow 'Sentinel dashboard (live)' @('scripts/dashboard.py', '--db', $script:CoordinatorDb, '--telemetry', $script:TelemetryDb, '--port', '0', '--open-browser')
    $collector = Start-PythonWindow 'Sentinel dashboard collector' @('scripts/observe_dashboard.py', '--context', $script:Context, '--cluster-id', $script:Cluster, '--namespace', $script:Namespace, '--db', $script:CoordinatorDb, '--telemetry', $script:TelemetryDb)
    $script:DashboardWindows = @($server, $collector)
    Write-Host 'PASS: Live dashboard and read-only collector started in separate windows.' -ForegroundColor Green
    Write-Host 'The browser opens once the frontend is built. Run option 3 to watch the fault and recovery.'
    Write-Host 'Stop each window with Ctrl+C before cleanup; the collector reports errors once the cluster is deleted.'
}

function Write-DashboardMarker([string]$Type, [string]$Summary) {
    # Markers only annotate the dashboard timeline; a failure here must not affect the test.
    try {
        Invoke-Python @('scripts/dashboard_event.py', '--telemetry', $script:TelemetryDb, '--member', '1', '--type', $Type, '--summary', $Summary) | Out-Null
    } catch {
        Write-Host ('Dashboard marker not recorded: ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

function Save-Deployment([string]$Label) {
    $raw = @(Invoke-Kube @('get', 'deployment/demo-api', '-o', 'json')) -join "`n"
    $raw | Set-Content -LiteralPath (Join-Path $script:RunDirectory ($Label + '-deployment.json')) -Encoding UTF8
    return ($raw | ConvertFrom-Json)
}

function Test-Health {
    Invoke-Kube @('exec', 'deployment/demo-api', '--', 'python3', '-c', "import urllib.request; r=urllib.request.urlopen('http://demo-api:8080/healthz', timeout=5); assert r.status == 200; print(r.read().decode())")
}

function Setup-Lab {
    Require-Tool 'kind' | Out-Null
    Require-Tool 'kubectl' | Out-Null
    Require-Docker
    $clusters = @(Invoke-Tool 'kind' @('get', 'clusters'))
    if ($clusters -contains $script:Cluster) {
        Write-Host 'Reusing the existing sentinel-lab cluster.'
    } else {
        Invoke-Tool 'kind' @('create', 'cluster', '--name', $script:Cluster, '--config', 'infra/kind.yaml', '--wait', '120s')
    }
    # Restore this named cluster's context if the user has removed its kubeconfig entry.
    Invoke-Tool 'kind' @('export', 'kubeconfig', '--name', $script:Cluster)
    Invoke-Tool 'docker' @('build', '-t', $script:Image, 'demo')
    Invoke-Tool 'kind' @('load', 'docker-image', $script:Image, '--name', $script:Cluster)
    Invoke-Kube @('apply', '-f', 'infra/demo.yaml')
    Invoke-Kube @('rollout', 'status', 'deployment/demo-api', '--timeout=120s')
    Test-Health
    Save-Deployment 'setup' | Out-Null
    Show-Status
    Write-Host 'PASS: Local cluster and demo application are ready.' -ForegroundColor Green
}

function Test-Offline {
    Invoke-Python @('scripts/check_contracts.py')
    Invoke-Python @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_*.py', '-v')
    Invoke-Python @('scripts/demo_coordinator.py')
    Invoke-Python @('scripts/demo_guard.py')
    Write-Host 'PASS: Python checks and offline demos completed.' -ForegroundColor Green
}

function Test-Observer([string]$Label, [int]$ExpectedIncidents, [int]$PollCount) {
    Find-Python
    Write-Host "Observing $Label for approximately $(($PollCount - 1) * 15) seconds..."
    $expectation = if ($ExpectedIncidents -eq 0) { 'healthy' } else { 'persistent-failure' }
    $reportPath = Join-Path $script:RunDirectory ($Label + '-observer.json')
    $raw = @(Invoke-Python @('scripts/demo_observer_live.py', '--context', $script:Context, '--poll-count', "$PollCount", '--interval-seconds', '15', '--expect', $expectation, '--output', $reportPath)) -join "`n"
    Write-Host $raw
    $raw | Set-Content -LiteralPath (Join-Path $script:RunDirectory ($Label + '-observer.json')) -Encoding UTF8
    $report = $raw | ConvertFrom-Json
    if (-not $report.validation.passed -or @($report.errors).Count -ne 0 -or $report.observations_read -ne $PollCount -or $report.incident_count -ne $ExpectedIncidents) {
        throw "Observer check failed: expected $ExpectedIncidents incidents, $PollCount observations and no collection errors."
    }
    Write-Host "PASS: $Label observer check." -ForegroundColor Green
}

function Restore-Baseline {
    if (-not (Test-Path -LiteralPath $script:BaselineFile)) { throw 'No saved baseline. Run Setup, then the live test first.' }
    $saved = Get-Content -LiteralPath $script:BaselineFile -Raw | ConvertFrom-Json
    if ($saved.cluster -ne $script:Cluster -or "$($saved.healthy_revision)" -notmatch '^\d+$') {
        throw 'Saved baseline is invalid for this lab.'
    }
    $current = Save-Deployment 'before-recovery'
    if ($current.metadata.uid -ne $saved.deployment_uid) { throw 'Deployment UID changed; the saved baseline cannot be used. Run Setup to restore the demo manifest.' }
    Invoke-Kube @('rollout', 'undo', 'deployment/demo-api', "--to-revision=$($saved.healthy_revision)")
    Invoke-Kube @('rollout', 'status', 'deployment/demo-api', '--timeout=120s')
    Test-Health
    Save-Deployment 'recovered' | Out-Null
    Write-Host 'PASS: Saved healthy revision restored.' -ForegroundColor Green
}

function Test-Live {
    Require-Docker
    Find-Python
    Invoke-Kube @('rollout', 'status', 'deployment/demo-api', '--timeout=120s')
    Test-Health
    Test-Observer 'healthy' 0 5
    $deployment = Save-Deployment 'healthy'
    $container = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq 'demo-api' })
    $setting = @($container.env | Where-Object { $_.name -eq 'STARTUP_FAILURE' })
    $revision = $deployment.metadata.annotations.'deployment.kubernetes.io/revision'
    if ($container.Count -ne 1 -or $setting.Count -ne 1 -or $setting[0].value -ne 'false' -or "$revision" -notmatch '^\d+$') {
        throw 'Deployment is not in the expected healthy baseline configuration. Run Setup first.'
    }
    $baseline = @{ cluster = $script:Cluster; deployment_uid = $deployment.metadata.uid; healthy_revision = [int]$revision }
    $baseline | ConvertTo-Json | Set-Content -LiteralPath $script:BaselineFile -Encoding UTF8
    Copy-Item -LiteralPath $script:BaselineFile -Destination (Join-Path $script:RunDirectory 'baseline.json')
    try {
        try {
            Write-Host 'Injecting STARTUP_FAILURE=true into the local demo...'
            Write-DashboardMarker 'fault' 'Lab: injecting STARTUP_FAILURE=true into deployment/demo-api'
            Invoke-Kube @('set', 'env', 'deployment/demo-api', '--containers=demo-api', 'STARTUP_FAILURE=true')
            Test-Observer 'failed' 1 9
            Write-DashboardMarker 'status' 'Lab: Observer detected the startup failure'
            Save-Deployment 'failed' | Out-Null
            Invoke-Kube @('get', 'pods')
        } finally {
            Write-Host 'Restoring the saved healthy revision, including after a failed test...'
            Write-DashboardMarker 'status' "Lab: restoring saved healthy revision $revision"
            Restore-Baseline
        }
        Test-Observer 'recovered' 0 5
    } catch {
        Write-DashboardMarker 'error' ('Lab live test failed: ' + $_.Exception.Message)
        throw
    }
    Write-DashboardMarker 'status' 'Lab: healthy -> failure detected -> recovered'
    Write-Host 'PASS: Healthy -> failure detected -> recovered.' -ForegroundColor Green
    Write-Host 'This is an operator-run live test; planner/executor/verifier automation is still separate.'
}

function Show-Status {
    Invoke-Kube @('get', 'nodes')
    Invoke-Kube @('get', 'deployments,pods,services')
}

function Cleanup-Lab {
    Require-Docker
    Invoke-Tool 'kind' @('delete', 'cluster', '--name', $script:Cluster)
    $remaining = @(Invoke-Tool 'kind' @('get', 'clusters'))
    Write-Host ('Remaining kind clusters: ' + ($remaining -join ', '))
    if ($remaining -contains $script:Cluster) { throw 'sentinel-lab is still present.' }
    $images = @(Invoke-Tool 'docker' @('image', 'ls', '--quiet', $script:Image))
    if ($images.Count -gt 0) {
        Invoke-Tool 'docker' @('image', 'rm', $script:Image)
    } else {
        Write-Host 'Demo image is already absent.'
    }
    Write-Host 'PASS: Local lab cleanup complete. Logs are retained under runs/.' -ForegroundColor Green
    Write-Host 'Stop any separate port-forward or dashboard windows with Ctrl+C. Docker Desktop can then be closed.'
}

function Run-Action([string]$SelectedAction) {
    switch ($SelectedAction) {
        'Setup' { Setup-Lab }
        'Test' { Test-Offline }
        'LiveTest' { Test-Live }
        'Status' { Show-Status }
        'Recover' { Restore-Baseline }
        'Cleanup' { Cleanup-Lab }
        'Full' { Setup-Lab; Test-Offline; Test-Live }
        'Dashboard' { Start-LiveDashboard }
        'Guard' {
            Invoke-Python @('-m', 'unittest', 'discover', '-s', 'tests', '-p', 'test_guard*.py', '-v')
            Invoke-Python @('scripts/demo_coordinator.py')
            Write-Host 'PASS: Guard unit/integration checks and coordinator demo.' -ForegroundColor Green
        }
    }
}

$exitCode = 0
$transcribing = $false
try {
    Start-Transcript -LiteralPath (Join-Path $script:RunDirectory 'terminal.log') | Out-Null
    $transcribing = $true
    Write-Host 'Sentinel LOCAL Kubernetes lab' -ForegroundColor Cyan
    Write-Host "Project: $projectRoot"
    Write-Host "Logs: $script:RunDirectory"
    Write-Host 'Keep Docker Desktop running. Cluster commands target kind-sentinel-lab.'
    if ($Action -ne 'Menu') {
        Run-Action $Action
    } else {
        while ($true) {
            Write-Host ''
            Write-Host '1 - Set up/reuse cluster and deploy demo'
            Write-Host '2 - Run Python tests and offline demos'
            Write-Host '3 - Run live healthy/failure/recovery test (about 4-5 minutes)'
            Write-Host '4 - Show cluster status'
            Write-Host '5 - Retry recovery using saved healthy revision'
            Write-Host '6 - Cleanup local cluster and demo image'
            Write-Host '7 - Setup + Python tests + live test (leaves cluster running)'
            Write-Host '8 - Run Recovery Guard unit/integration tests and coordinator demo'
            Write-Host '9 - Open live dashboard and collector for this cluster (read-only, new windows)'
            Write-Host '0 - Leave menu (terminal stays open)'
            $selection = Read-Host 'Choose an option'
            if ($selection -eq '0' -or $null -eq $selection) { break }
            $options = @{ '1'='Setup'; '2'='Test'; '3'='LiveTest'; '4'='Status'; '5'='Recover'; '6'='Cleanup'; '7'='Full'; '8'='Guard'; '9'='Dashboard' }
            if (-not $options.ContainsKey($selection)) { Write-Host 'Choose 0 through 9.'; continue }
            try { Run-Action $options[$selection] } catch {
                Write-Host ('FAILED: ' + $_.Exception.Message) -ForegroundColor Red
                Write-Host 'Review the output above. The menu remains available to retry or recover.'
            }
        }
    }
} catch {
    Write-Host ('FAILED: ' + $_.Exception.Message) -ForegroundColor Red
    $exitCode = 1
} finally {
    if ($transcribing) { Stop-Transcript | Out-Null }
}
exit $exitCode
