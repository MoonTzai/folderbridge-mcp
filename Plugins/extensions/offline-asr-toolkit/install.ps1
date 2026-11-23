[CmdletBinding()]
param(
    [string]$DestinationRoot = "",
    [string]$ProfilePath = ""
)

$ErrorActionPreference = "Stop"
$extensionId = "offline-asr-toolkit"
$sourceRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

if (-not $DestinationRoot) {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is unavailable. Pass -DestinationRoot explicitly."
    }
    $DestinationRoot = Join-Path $env:LOCALAPPDATA "folderbridge-mcp\extensions"
}

$destinationRootPath = [IO.Path]::GetFullPath($DestinationRoot)
$target = Join-Path $destinationRootPath $extensionId
New-Item -ItemType Directory -Path $destinationRootPath -Force | Out-Null

function Assert-RegularFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Required local file is missing: $Path"
    }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Linked/reparse source file is denied: $Path"
    }
}

if (Test-Path -LiteralPath $target) {
    $targetItem = Get-Item -LiteralPath $target -Force
    if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $targetItem.PSIsContainer) {
        throw "Extension target is not a regular directory: $target"
    }
    if ([IO.Path]::GetFullPath($targetItem.FullName) -eq [IO.Path]::GetFullPath($sourceRoot)) {
        Write-Host "Already running from the target hot-load directory: $target"
        exit 0
    }
}

$profileSource = ""
if ($ProfilePath) {
    $profileSource = [IO.Path]::GetFullPath($ProfilePath)
}
elseif (Test-Path -LiteralPath (Join-Path $target "profiles.json") -PathType Leaf) {
    # Preserve the previously approved machine-local provider configuration.
    $profileSource = Join-Path $target "profiles.json"
}
if ($profileSource) {
    Assert-RegularFile $profileSource
    $profileData = Get-Content -LiteralPath $profileSource -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($profileData.schema -ne "offline-asr-profiles-v1") {
        throw "Unexpected local profile schema; refuse install."
    }
}

$nonce = [Guid]::NewGuid().ToString("N")
$stage = Join-Path $destinationRootPath (".$extensionId.install-" + $nonce)
$backup = Join-Path $destinationRootPath (".$extensionId.backup-" + $nonce)
$backupCreated = $false

try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    foreach ($name in @("folderbridge-extension.json", "plugin.py", "asr_worker.py", "README.md", "install.ps1")) {
        $source = Join-Path $sourceRoot $name
        Assert-RegularFile $source
        Copy-Item -LiteralPath $source -Destination (Join-Path $stage $name)
    }
    if ($profileSource) {
        Copy-Item -LiteralPath $profileSource -Destination (Join-Path $stage "profiles.json")
    }
    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $backup
        $backupCreated = $true
    }
    Move-Item -LiteralPath $stage -Destination $target
    Write-Host "Installed external Offline ASR Toolkit: $target"
    Write-Host "No approval was performed by this script."
    Write-Host "Open FolderBridge Extensions & Skills, rescan, review the actual hash + permissions, approve and enable."
    if (-not $profileSource) {
        Write-Warning "No profiles.json supplied: plugin can load, but cannot transcribe until a pinned provider profile is installed and reapproved."
    }
    if ($backupCreated -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Recurse -Force
        $backupCreated = $false
    }
}
catch {
    if ((Test-Path -LiteralPath $stage) -and -not (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($backupCreated -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
        $backupCreated = $false
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
