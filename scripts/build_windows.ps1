[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot "release\windows-x64"
$workDir = Join-Path $projectRoot ".build\pyinstaller"
$specDir = Join-Path $projectRoot ".build"

Push-Location $projectRoot
try {
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Install requirements-build.txt in an isolated build environment first."
    }

    & $Python -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --console `
        --hide-console hide-early `
        --noupx `
        --name FolderBridge `
        --version-file (Join-Path $projectRoot "packaging\windows_version_info.txt") `
        --manifest (Join-Path $projectRoot "packaging\windows_dpi_manifest.xml") `
        --distpath $releaseDir `
        --workpath $workDir `
        --specpath $specDir `
        (Join-Path $projectRoot "folderbridge_launcher.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $executable = Join-Path $releaseDir "FolderBridge.exe"
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
    "$hash *FolderBridge.exe" | Set-Content -LiteralPath (Join-Path $releaseDir "FolderBridge.exe.sha256") -Encoding ascii
    Write-Host "Built: $executable"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
