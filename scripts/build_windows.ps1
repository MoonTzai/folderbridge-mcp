[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

# Approved FolderBridge tasks intentionally run with a minimal environment.
# Older packaged runners may omit Windows home-directory variables entirely,
# while PyInstaller/Path.home() still requires USERPROFILE. Recover it from
# the Windows user profile API instead of trusting an inherited path.
if (-not $env:USERPROFILE) {
    $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if (-not $userProfile) {
        throw "Cannot determine the current Windows user profile directory."
    }
    $env:USERPROFILE = $userProfile
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot "release\windows-x64"
$workDir = Join-Path $projectRoot ".build\pyinstaller"
$specDir = Join-Path $projectRoot ".build"
$bundledExtensions = @("git-publisher", "office", "skill-engine")
$bundledSkillPacks = @("matt-pocock-engineering")

Push-Location $projectRoot
try {
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Install requirements-build.txt in an isolated build environment first."
    }

    $pyInstallerArgs = @(
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--hide-console", "hide-early",
        "--noupx",
        "--name", "FolderBridge",
        "--version-file", (Join-Path $projectRoot "packaging\windows_version_info.txt"),
        "--manifest", (Join-Path $projectRoot "packaging\windows_dpi_manifest.xml")
    )
    foreach ($extensionId in $bundledExtensions) {
        $source = Join-Path $projectRoot "extensions\$extensionId"
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Missing bundled Extension source: $extensionId"
        }
        $pyInstallerArgs += @("--add-data", ($source + ";extensions\" + $extensionId))
    }
    foreach ($packId in $bundledSkillPacks) {
        $source = Join-Path $projectRoot "skill_packs\$packId"
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "Missing bundled Skill Pack source: $packId"
        }
        $pyInstallerArgs += @("--add-data", ($source + ";skill_packs\" + $packId))
    }
    $pyInstallerArgs += @(
        "--hidden-import", "folderbridge_mcp.extension_worker",
        "--distpath", $releaseDir,
        "--workpath", $workDir,
        "--specpath", $specDir,
        (Join-Path $projectRoot "folderbridge_launcher.py")
    )
    & $Python -m PyInstaller @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $executable = Join-Path $releaseDir "FolderBridge.exe"
    $bundleVerifier = Join-Path $projectRoot "scripts\verify_windows_bundle.py"
    $bundleSmoke = (& $Python $bundleVerifier $executable 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Built executable bundle verification failed: $bundleSmoke"
    }
    $smoke = (& $executable --version 2>&1 | Out-String).Trim()
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
    "$hash *FolderBridge.exe" | Set-Content -LiteralPath (Join-Path $releaseDir "FolderBridge.exe.sha256") -Encoding ascii
    Write-Host "Built: $executable"
    Write-Host "Smoke: $smoke"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
