[CmdletBinding()]
param(
    [string]$DestinationRoot = ""
)

$ErrorActionPreference = "Stop"
$extensionId = "storyboard-chatgpt-web"
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
        Write-Host "Storyboard ChatGPT Web is already running from the target hot-load directory: $target"
        exit 0
    }
}

$required = @(
    "folderbridge-extension.json",
    "plugin.py",
    "bridge.cjs",
    "live_judge.py",
    "live_generator.py",
    "live_state.py",
    "README.md",
    "install.ps1",
    "engine\bundle-manifest.json",
    "engine\compiler\storyboard-forge-core.js",
    "engine\runner\compiled-task-adapter.cjs",
    "engine\runner\execution-handoff.cjs",
    "engine\runner\judge-contract.cjs",
    "engine\runner\operation-identity.cjs",
    "engine\runner\reference-binder.cjs",
    "engine\runner\repair-execution.cjs",
    "engine\runner\state-machine.cjs"
)

foreach ($relative in $required) {
    $source = Join-Path $sourceRoot $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "Required Extension file is missing: $relative"
    }
    $item = Get-Item -LiteralPath $source -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing to install a reparse-point source file: $relative"
    }
}

$nonce = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $destinationRootPath (".$extensionId.install-" + $nonce)
$backup = Join-Path $destinationRootPath (".$extensionId.backup-" + $nonce)
$backupCreated = $false

try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    foreach ($relative in $required) {
        $source = Join-Path $sourceRoot $relative
        $destination = Join-Path $staging $relative
        $parent = Split-Path -Parent $destination
        if ($parent) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
        Copy-Item -LiteralPath $source -Destination $destination
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

    Write-Host "Installed external Storyboard ChatGPT Web Extension: $target"
    Write-Host "Open FolderBridge Extensions & Skills, click Rescan, inspect the displayed exact hash/permissions, approve it, then enable it."
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
