$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'video-storyboard-production'
if (-not (Test-Path -LiteralPath $src -PathType Container)) { throw "Source pack not found: $src" }
$root = Join-Path $env:LOCALAPPDATA 'folderbridge-mcp\skill-packs'
$dst = Join-Path $root 'video-storyboard-production'
New-Item -ItemType Directory -Force -Path $root | Out-Null
if (Test-Path -LiteralPath $dst) {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = "$dst.backup-$stamp"
    Move-Item -LiteralPath $dst -Destination $backup
    Write-Host "Backed up previous pack to: $backup"
}
Copy-Item -LiteralPath $src -Destination $dst -Recurse
Write-Host "Installed external Skill Pack to: $dst"
Write-Host "Next: FolderBridge -> Extensions/Skills -> Rescan -> review displayed SHA-256 -> approve exact hash -> enable."
