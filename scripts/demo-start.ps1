[CmdletBinding()]
param([switch]$SkipBuild, [switch]$Offline, [switch]$Unattended, [switch]$GuardRejection, [switch]$UIControls,
      [switch]$KeepCluster, [ValidateRange(1024,65535)][int]$Port = 8765)

$ErrorActionPreference = 'Stop'
if ($UIControls -and $Unattended) { throw '-UIControls waits for browser input and cannot use -Unattended.' }
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
. (Join-Path $PSScriptRoot 'packaged-demo-common.ps1')
Initialize-PackagedTools

# Refuse existing reserved resources before creating or modifying anything.
$clusters = @(& $DemoKind get clusters)
if ($LASTEXITCODE -ne 0) { throw 'Could not list kind clusters.' }
if ($clusters -contains $PackagedCluster) { throw 'Packaged cluster already exists. Inspect it or run scripts\demo-stop.ps1 first.' }
$containers = @(& $DemoDocker ps -a --filter "name=^/$PackagedContainer$" --format '{{.Names}}')
if ($LASTEXITCODE -ne 0 -or $containers -contains $PackagedContainer) { throw 'Packaged runner name is unavailable.' }

if (-not $SkipBuild -and -not $Offline) {
    $sourceRevision = 'unknown'
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $revision = & git rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0) { $sourceRevision = $revision }
    }
    & $DemoDocker build --pull --build-arg "SOURCE_REVISION=$sourceRevision" --label "org.opencontainers.image.version=$PackagedVersion" -f deploy/docker/demo-runner.Dockerfile -t $PackagedImage .
    if ($LASTEXITCODE -ne 0) { throw 'Demo image build failed.' }
}
& $DemoDocker image inspect $PackagedImage --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'Runner image unavailable. Build online or docker load the exported image first.' }

if (-not $Offline) {
    foreach ($image in @($ShippingImage, $ProbeImage, 'kindest/node:v1.37.0')) {
        & $DemoDocker pull $image
        if ($LASTEXITCODE -ne 0) { throw "Image preparation failed: $image" }
    }
}
$shipping = Get-PackagedDigest $ShippingImage
$client = Get-PackagedDigest $ProbeImage
& $DemoDocker image inspect kindest/node:v1.37.0 --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw 'The pinned kind node image must be cached for offline setup.' }

$runName = 'packaged-boutique-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0,6)
$runRoot = Join-Path $projectRoot ('data\sentinel\runs\' + $runName)
$bootstrap = Join-Path $runRoot 'bootstrap'
$data = Join-Path $runRoot 'data'
New-Item -ItemType Directory -Path $bootstrap, $data -Force | Out-Null
$credential = Join-Path $bootstrap 'operator-kubeconfig.yaml'
$resultCode = 1
$created = $false
try {
    $created = $true
    $previousContext = Get-KubeCurrentContext
    try {
        & $DemoKind create cluster --name $PackagedCluster --image kindest/node:v1.37.0 --config deploy/kind/kind.yaml --wait 120s
        $createExit = $LASTEXITCODE
    } finally { Restore-KubeCurrentContext $previousContext ('kind-' + $PackagedCluster) }
    if ($createExit -ne 0) { throw 'Packaged kind cluster creation failed.' }
    $node = $PackagedCluster + '-control-plane'
    foreach ($item in @(@{Name='shipping';Image=$ShippingImage;Info=$shipping}, @{Name='client';Image=$ProbeImage;Info=$client})) {
        $archive = Join-Path $bootstrap ($item.Name + '.tar')
        & $DemoDocker image save -o $archive $item.Image
        if ($LASTEXITCODE -ne 0) { throw 'Could not export cached workload image.' }
        Import-PackagedImage -Archive $archive -Node $node -Architecture $item.Info.Architecture
        $source = $item.Image
        if ($source -eq $ProbeImage) { $source = 'docker.io/' + $source }
        & $DemoDocker exec $node ctr --namespace k8s.io images tag --force $source $item.Info.Digest
        if ($LASTEXITCODE -ne 0) { throw 'Could not register immutable workload reference.' }
        Remove-Item -LiteralPath $archive
    }
    $configuration = & $DemoKind get kubeconfig --name $PackagedCluster --internal
    if ($LASTEXITCODE -ne 0) { throw 'Cannot export internal TLS kubeconfig.' }
    # Restrict the initially empty file before putting administrator credentials in it.
    New-Item -ItemType File -Path $credential -Force | Out-Null
    $acl = Get-Acl -LiteralPath $credential
    $acl.SetAccessRuleProtection($true, $false)
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
    $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($identity,'FullControl','Allow')))
    Set-Acl -LiteralPath $credential -AclObject $acl
    [IO.File]::WriteAllText($credential, ($configuration -join "`n"), (New-Object Text.UTF8Encoding($false)))
    $environment = @{cluster_id=$PackagedCluster;context=('kind-' + $PackagedCluster);node_name=$node;shipping=$shipping.Digest;client=$client.Digest}
    $environment | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bootstrap 'environment.json') -Encoding UTF8
    $imageId = & $DemoDocker image inspect $PackagedImage --format '{{.Id}}'
    @{image=$PackagedImage;image_id=$imageId;cluster=$PackagedCluster;url="http://127.0.0.1:$Port";offline=[bool]$Offline} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot 'package-session.json') -Encoding UTF8
    Write-Host "Packaged demo evidence: $runRoot"
    Write-Host "Open the live dashboard: http://127.0.0.1:$Port"
    $runOptions = @('run','--rm','--name',$PackagedContainer,'--label','sentinel.demo=boutique-package',
        '--network','kind','--init','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--read-only','--tmpfs','/tmp:rw,nosuid,nodev,size=64m',
        '--mount',"type=bind,source=$bootstrap,target=/bootstrap,readonly",
        '--mount',"type=bind,source=$data,target=/data",
        '--env','KUBECONFIG=/bootstrap/operator-kubeconfig.yaml',
        '--publish',"127.0.0.1:${Port}:8765")
    # Interactive approval is typed at this terminal, so every attended run attaches one.
    if (-not $Unattended) { $runOptions += '-it' }
    $runOptions += @($PackagedImage,'--prepared-environment','/bootstrap/environment.json','--run-dir','/data/evidence',
        '--dashboard','--dashboard-bind','0.0.0.0','--dashboard-port','8765')
    # -Unattended is the explicit opt-in to automated lab approval, recorded as such in summary.json.
    $runOptions += @('--approval', $(if ($Unattended) { 'auto-lab' } else { 'interactive' }))
    if ($GuardRejection) { $runOptions += '--guard-rejection' }
    if ($UIControls) { $runOptions += '--ui-controls' }
    elseif ($Unattended) { $runOptions += '--exit-after-test' }
    else { $runOptions += '--pause-before-injection' }
    & $DemoDocker @runOptions
    $resultCode = $LASTEXITCODE
} finally {
    try {
        if ($created) { Stop-PackagedResources -KeepCluster:$KeepCluster }
    } finally {
        if (Test-Path -LiteralPath $credential) { Remove-Item -LiteralPath $credential }
    }
    @{cluster_removed=(-not [bool]$KeepCluster);operator_credentials_removed=(-not (Test-Path -LiteralPath $credential));exit_code=$resultCode} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $runRoot 'host-cleanup.json') -Encoding UTF8
}
exit $resultCode
