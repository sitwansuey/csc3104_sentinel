# Keep the user's kubectl current-context when a launcher creates or deletes a kind
# cluster. kind selects a new cluster on create and leaves no current-context after
# deleting the current one. The launchers pass --context explicitly, so they need
# neither change. These helpers are best effort: they never fail a launcher.

function Invoke-KubeConfig([string[]]$Arguments) {
    $kubectl = Get-Command kubectl.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $kubectl) { return $null }
    # Use the process API: native stderr must not become a PowerShell error under 'Stop'.
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $kubectl.Source
    $process.StartInfo.Arguments = ($Arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    try {
        if (-not $process.Start()) { return $null }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $null = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) { $process.Kill(); return $null }
        if ($process.ExitCode -ne 0) { return $null }
        return $stdout.Result.Trim()
    } catch {
        return $null
    } finally {
        $process.Dispose()
    }
}

function Get-KubeCurrentContext {
    $context = Invoke-KubeConfig @('config', 'current-context')
    if ($context) { return $context }
    return $null
}

function Restore-KubeCurrentContext([string]$Previous, [string]$LabContext) {
    # If the user had selected the lab itself there is nothing else to restore.
    if ($Previous -and $Previous -eq $LabContext) { return }
    if ($Previous) {
        $null = Invoke-KubeConfig @('config', 'use-context', $Previous)
    } else {
        $null = Invoke-KubeConfig @('config', 'unset', 'current-context')
    }
}
