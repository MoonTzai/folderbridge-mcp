[CmdletBinding()]
param([string]$DestinationRoot = "")

$ErrorActionPreference = "Stop"
$extensionId = "exe-packager-toolkit"
$sourceRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

if (-not $DestinationRoot) {
    if (-not $env:LOCALAPPDATA) {
        throw "LOCALAPPDATA is unavailable; pass -DestinationRoot manually."
    }
    $DestinationRoot = Join-Path $env:LOCALAPPDATA "folderbridge-mcp\extensions"
}
$destRoot = [IO.Path]::GetFullPath($DestinationRoot)
$target = Join-Path $destRoot $extensionId
$sourceReal = [IO.Path]::GetFullPath($sourceRoot)

New-Item -ItemType Directory -Path $destRoot -Force | Out-Null
if (Test-Path -LiteralPath $target) {
    $item = Get-Item -LiteralPath $target -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or -not $item.PSIsContainer) {
        throw "Extension destination must be a regular directory, not a symlink/junction."
    }
    if ([IO.Path]::GetFullPath($item.FullName) -eq $sourceReal) {
        throw "Source and destination are identical. Nothing was installed."
    }
}
$nonce = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $destRoot (".$extensionId.stage-" + $nonce)
$backup = Join-Path $destRoot (".$extensionId.backup-" + $nonce)
$backupReady = $false

try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    foreach ($file in @("folderbridge-extension.json", "plugin.py", "README.md", "install.ps1")) {
        $from = Join-Path $sourceRoot $file
        if (-not (Test-Path -LiteralPath $from -PathType Leaf)) {
            throw "Missing required plugin file: $file"
        }
        Copy-Item -LiteralPath $from -Destination (Join-Path $staging $file)
    }
    $test = Join-Path $sourceRoot "tests\test_plugin.py"
    if (Test-Path -LiteralPath $test -PathType Leaf) {
        $testDir = Join-Path $staging "tests"
        New-Item -ItemType Directory -Path $testDir | Out-Null
        Copy-Item -LiteralPath $test -Destination (Join-Path $testDir "test_plugin.py")
    }
    if (Test-Path -LiteralPath $target) {
        Move-Item -LiteralPath $target -Destination $backup
        $backupReady = $true
    }
    Move-Item -LiteralPath $staging -Destination $target
    Write-Host "Extension copied: $target"
    Write-Host "NEXT: Open FolderBridge -> Extensions & Skills -> Rescan -> Approve exact hash -> Enable."
    Write-Host "No plugin approval or execution has been performed by this installer."
    if ($backupReady -and (Test-Path -LiteralPath $backup)) {
        Remove-Item -LiteralPath $backup -Recurse -Force
        $backupReady = $false
    }
}
catch {
    if ($backupReady -and -not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) {
        Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
