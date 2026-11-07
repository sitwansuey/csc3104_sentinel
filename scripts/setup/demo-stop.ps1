[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
. (Join-Path $PSScriptRoot 'packaged-demo-common.ps1')
Initialize-PackagedTools
Stop-PackagedResources
Write-Host 'Packaged runner and sentinel-boutique-package cluster stopped. Evidence and image caches remain.'
