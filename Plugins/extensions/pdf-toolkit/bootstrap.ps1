param(
    [switch]$RefreshUpstreams,
    [switch]$ForceInstall
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

Write-Host '=== PDF Toolkit research snapshots ==='
& (Join-Path $PSScriptRoot 'fetch-upstreams.ps1') -Refresh:$RefreshUpstreams
if ($LASTEXITCODE -ne 0) { throw "fetch-upstreams.ps1 failed with exit code $LASTEXITCODE" }

Write-Host ''
Write-Host '=== PDF Toolkit external extension ==='
& (Join-Path $PSScriptRoot 'install.ps1') -Force:$ForceInstall
if ($LASTEXITCODE -ne 0) { throw "install.ps1 failed with exit code $LASTEXITCODE" }
