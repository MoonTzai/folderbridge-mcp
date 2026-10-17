[CmdletBinding()]
param(
    [string]$DestinationRoot = ""
)

$ErrorActionPreference = "Stop"
$extensionId = "gpt-sovits-local"
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

if (Test-Path -LiteralPath $target) {
    $targetItem = Get-Item -LiteralPath $target -Force
    if (($targetItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to replace a reparse-point Extension target: $target"
    }
    if (-not $targetItem.PSIsContainer) {
        throw "Extension target exists but is not a directory: $target"
    }
    if ([IO.Path]::GetFullPath($targetItem.FullName) -eq [IO.Path]::GetFullPath($sourceRoot)) {
        Write-Host "Local GPT-SoVITS Extension is already running from the target directory: $target"
        exit 0
    }
}

$nonce = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $destinationRootPath (".$extensionId.install-" + $nonce)
$backup = Join-Path $destinationRootPath (".$extensionId.backup-" + $nonce)
$backupCreated = $false

try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    foreach ($name in @("folderbridge-extension.json", "plugin.py", "README.md", "install.ps1")) {
        $source = Join-Path $sourceRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Required Extension file is missing: $name"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $staging $name)
    }

    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $backup
        $backupCreated = $true
    }

    Move-Item -LiteralPath $staging -Destination $target
    if ($backupCreated -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Recurse -Force
        $backupCreated = $false
    }

    Write-Host "Installed external Local GPT-SoVITS Extension: $target"
    Write-Host "Open FolderBridge Extensions & Skills, click Rescan, approve the exact new hash, then enable it."
}
catch {
    if ((Test-Path -LiteralPath $staging) -and -not (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($backupCreated -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
        $backupCreated = $false
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
