# Shared fixed scope for the trusted host bootstrap and teardown scripts.
. (Join-Path $PSScriptRoot 'kube-context.ps1')
$script:PackagedCluster = 'sentinel-boutique-package'
$script:PackagedContainer = 'sentinel-boutique-runner'
$script:PackagedImage = 'sentinel-boutique-runner:0.3.0'
$script:ShippingImage = 'us-central1-docker.pkg.dev/online-boutique-ci/microservices-demo/shippingservice:v0.10.7'
$script:ProbeImage = 'fullstorydev/grpcurl:v1.9.3'

function Initialize-PackagedTools {
    . (Join-Path $PSScriptRoot 'install-dependencies.ps1') -FunctionsOnly
    Refresh-ToolPaths
    $script:DemoDocker = (Get-Command docker.exe -ErrorAction Stop).Source
    $script:DemoKind = (Get-Command kind.exe -ErrorAction Stop).Source
    $engine = & $script:DemoDocker info --format '{{.OSType}}'
    if ($LASTEXITCODE -ne 0 -or $engine -ne 'linux') { throw 'Start Docker Desktop with Linux containers.' }
}

function Get-PackagedDigest([string]$Image) {
    $inspection = & $script:DemoDocker image inspect $Image
    if ($LASTEXITCODE -ne 0) { throw "Image unavailable: $Image. Prepare online before using -Offline." }
    $record = @($inspection | ConvertFrom-Json)[0]
    $repository = $Image.Substring(0, $Image.LastIndexOf(':'))
    $digests = @($record.RepoDigests | ForEach-Object { $_ -replace '^docker.io/', '' } |
        Where-Object { $_ -match ('^' + [regex]::Escape($repository) + '@sha256:[a-f0-9]{64}$') } | Select-Object -Unique)
    if ($digests.Count -ne 1) { throw "Cannot resolve exact repository manifest for $Image" }
    $digest = $digests[0]
    if ($Image -eq $script:ProbeImage) { $digest = 'docker.io/' + $digest }
    return @{ Digest = $digest; Architecture = $record.Architecture }
}

function Import-PackagedImage([string]$Archive, [string]$Node, [string]$Architecture) {
    if ($Node -ne ($script:PackagedCluster + '-control-plane') -or $Architecture -notin @('amd64','arm64')) {
        throw 'Unexpected node or image architecture.'
    }
    # Stream bytes directly. PowerShell pipelines can corrupt binary tar data;
    # copying into a running node's temporary filesystem is also unreliable.
    $start = New-Object Diagnostics.ProcessStartInfo
    $start.FileName = $script:DemoDocker
    $start.Arguments = "exec -i $Node ctr --namespace k8s.io images import --platform linux/$Architecture --digests -"
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $start
    # Windows PowerShell 5.1 creates the redirected input writer with the console input
    # encoding. Under code page 65001 that writes a UTF-8 byte-order mark ahead of the
    # tar stream and ctr rejects the archive. Use UTF-8 without a BOM while starting.
    $previousEncoding = $null
    try { $previousEncoding = [Console]::InputEncoding; [Console]::InputEncoding = New-Object Text.UTF8Encoding $false } catch { $previousEncoding = $null }
    try {
        [void]$process.Start()
        $stream = [IO.File]::OpenRead($Archive)
        try { $stream.CopyTo($process.StandardInput.BaseStream) } finally { $stream.Dispose(); $process.StandardInput.Close() }
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw 'Could not import cached image for the selected architecture.' }
    } finally {
        $process.Dispose()
        if ($null -ne $previousEncoding) { try { [Console]::InputEncoding = $previousEncoding } catch { } }
    }
}

function Stop-PackagedResources([switch]$KeepCluster) {
    # Never stop an unrelated container that happens to use the reserved name.
    $containers = @(& $script:DemoDocker ps -a --filter "name=^/$script:PackagedContainer$" --format '{{.Names}}')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect packaged demo container.' }
    if ($containers -contains $script:PackagedContainer) {
        $inspection = & $script:DemoDocker inspect $script:PackagedContainer
        if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect the runner ownership label.' }
        $record = @($inspection | ConvertFrom-Json)[0]
        if ($record.Config.Labels.'sentinel.demo' -ne 'boutique-package') { throw 'Refusing to stop a container without the demo ownership label.' }
        & $script:DemoDocker stop --timeout 45 $script:PackagedContainer | Out-Host
        if ($LASTEXITCODE -ne 0) { throw 'Could not stop packaged demo runner.' }
        $remainingContainers = @(& $script:DemoDocker ps -a --filter "name=^/$script:PackagedContainer$" --format '{{.Names}}')
        if ($remainingContainers -contains $script:PackagedContainer) {
            & $script:DemoDocker rm $script:PackagedContainer | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Could not remove stopped demo runner.' }
        }
    }
    if ($KeepCluster) { return }
    $clusters = @(& $script:DemoKind get clusters)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect kind clusters.' }
    if ($clusters -contains $script:PackagedCluster) {
        $previousContext = Get-KubeCurrentContext
        try {
            & $script:DemoKind delete cluster --name $script:PackagedCluster
            $deleteExit = $LASTEXITCODE
        } finally { Restore-KubeCurrentContext $previousContext ('kind-' + $script:PackagedCluster) }
        if ($deleteExit -ne 0) { throw 'Could not delete the packaged demo cluster.' }
    }
    $remaining = @(& $script:DemoKind get clusters)
    if ($LASTEXITCODE -ne 0 -or $remaining -contains $script:PackagedCluster) { throw 'Packaged cluster cleanup could not be verified.' }
}
