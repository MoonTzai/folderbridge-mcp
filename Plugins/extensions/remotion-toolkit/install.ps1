[CmdletBinding()]
param([string]$DestinationRoot = "")
$ErrorActionPreference = "Stop"
$id = "remotion-toolkit"
$source = (Resolve-Path -LiteralPath $PSScriptRoot).Path
if (-not $DestinationRoot) {
  if (-not $env:LOCALAPPDATA) { throw "LOCALAPPDATA unavailable; pass DestinationRoot." }
  $DestinationRoot = Join-Path $env:LOCALAPPDATA "folderbridge-mcp\extensions"
}
$root = [IO.Path]::GetFullPath($DestinationRoot)
$target = Join-Path $root $id
$files = @("folderbridge-extension.json", "plugin.py", "bridge.cjs", "README.md", "install.ps1")
foreach ($file in $files) {
  if (-not (Test-Path -LiteralPath (Join-Path $source $file) -PathType Leaf)) { throw "Required plugin file missing: $file" }
}
New-Item -ItemType Directory -Force -Path $root | Out-Null
if (Test-Path -LiteralPath $target) {
  $item = Get-Item -LiteralPath $target -Force
  if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Unsafe existing target." }
  if ([IO.Path]::GetFullPath($item.FullName) -eq [IO.Path]::GetFullPath($source)) {
    Write-Host "Remotion Toolkit already in hot-load directory; use FB Rescan and approve exact hash."
    exit 0
  }
}
$nonce = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $root (".$id.stage-$nonce")
$backupRoot = Join-Path (Split-Path $root -Parent) "extension-backups"
$backup = Join-Path $backupRoot ("$id-$nonce")
$hadBackup = $false
try {
  New-Item -ItemType Directory -Path $staging | Out-Null
  foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $source $file) -Destination (Join-Path $staging $file) }
  $testSrc = Join-Path $source "tests\test_plugin.py"
  if (Test-Path -LiteralPath $testSrc -PathType Leaf) {
    New-Item -ItemType Directory -Path (Join-Path $staging "tests") | Out-Null
    Copy-Item -LiteralPath $testSrc -Destination (Join-Path $staging "tests\test_plugin.py")
  }
  # Validate the manifest locally without executing third-party code.
  $manifest = Get-Content -LiteralPath (Join-Path $staging "folderbridge-extension.json") -Raw -Encoding UTF8 | ConvertFrom-Json
  if ($manifest.id -ne $id -or $manifest.entrypoint -ne "plugin.py") { throw "Manifest identity mismatch." }
  if (Test-Path -LiteralPath $target) {
    New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
    Move-Item -LiteralPath $target -Destination $backup
    $hadBackup = $true
  }
  Move-Item -LiteralPath $staging -Destination $target
  Write-Host "Remotion Toolkit local source installed: $target"
  if ($hadBackup) { Write-Host "Rollback backup preserved: $backup" }
  Write-Host "In FolderBridge: Extensions & Skills -> Rescan -> review permissions -> approve new exact directory SHA -> Enable."
  Write-Host "No network, npm installation, Remotion render or user config approvals were performed."
} catch {
  if ($hadBackup -and -not (Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $backup)) {
    Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
  }
  throw
} finally {
  if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue }
}
