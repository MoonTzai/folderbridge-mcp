[CmdletBinding()]
param(
    [string]$DestinationRoot = ""
)

$ErrorActionPreference = "Stop"
$extensionId = "testonline-host"
$source = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if (-not $DestinationRoot) {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is unavailable. Specify -DestinationRoot."
    }
    $DestinationRoot = Join-Path $env:LOCALAPPDATA "folderbridge-mcp\extensions"
}
$root = [IO.Path]::GetFullPath($DestinationRoot)
$target = Join-Path $root $extensionId
$files = @(
    "folderbridge-extension.json",
    "plugin.py",
    "server.py",
    "tls_cert.py",
    "README.md",
    "install.ps1"
)
foreach ($name in $files) {
    $item = Join-Path $source $name
    if (-not (Test-Path -LiteralPath $item -PathType Leaf)) {
        throw "Missing required TestOnline plugin file: $item"
    }
}
New-Item -ItemType Directory -Force -Path $root | Out-Null
if (Test-Path -LiteralPath $target) {
    $item = Get-Item -LiteralPath $target -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Refusing to replace a linked extension destination."
    }
    if (-not $item.PSIsContainer) {
        throw "Extension destination is not a directory."
    }
    if ([IO.Path]::GetFullPath($item.FullName) -eq $source) {
        Write-Host "TestOnline plugin is already in the extension directory."
        exit 0
    }
}
$nonce = [Guid]::NewGuid().ToString("N")
$stage = Join-Path $root (".$extensionId.stage-" + $nonce)
$backup = Join-Path $root (".$extensionId.backup-" + $nonce)
$backupCreated = $false
try {
    New-Item -ItemType Directory -Path $stage | Out-Null
    foreach ($name in $files) {
        Copy-Item -LiteralPath (Join-Path $source $name) -Destination (Join-Path $stage $name)
        $original = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $source $name)).Hash
        $copied = (Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $stage $name)).Hash
        if ($original -ne $copied) { throw "Staged file SHA mismatch: $name" }
    }
    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $backup
        $backupCreated = $true
    }
    Move-Item -LiteralPath $stage -Destination $target
    if ($backupCreated) {
        Remove-Item -LiteralPath $backup -Recurse -Force
        $backupCreated = $false
    }
    Write-Host "Installed TestOnline external plugin: $target"
    Write-Host "In FolderBridge: Extensions & Skills -> Rescan -> Approve SHA -> Enable."
    Write-Host "The Launcher then starts TestOnline automatically and stops it on exit."
}
catch {
    if ($backupCreated -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
        $backupCreated = $false
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
