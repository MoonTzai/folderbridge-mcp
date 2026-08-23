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
        --add-data ((Join-Path $projectRoot "extensions") + ";extensions") `
        --add-data ((Join-Path $projectRoot "skill_packs") + ";skill_packs") `
        --hidden-import folderbridge_mcp.comfyui `
        --hidden-import folderbridge_mcp.extension_worker `
        --distpath $releaseDir `
        --workpath $workDir `
        --specpath $specDir `
        (Join-Path $projectRoot "folderbridge_launcher.py")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE."
    }

    $executable = Join-Path $releaseDir "FolderBridge.exe"
    $smoke = (& $executable --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $smoke) {
        throw "Built executable smoke test failed."
    }
    $extensionSmoke = (& $executable extensions --json 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $extensionSmoke -notmatch '"id"\s*:\s*"comfyui"' -or $extensionSmoke -notmatch '"id"\s*:\s*"office"' -or $extensionSmoke -notmatch '"id"\s*:\s*"git-publisher"' -or $extensionSmoke -notmatch '"id"\s*:\s*"skill-engine"') {
        throw "Built executable extension smoke test failed: all bundled extensions must be discovered."
    }
    $skillSmoke = (& $executable skills --json 2>&1 | Out-String).Trim()
    $requiredSkillPatterns = @(
        '"id"\s*:\s*"folderbridge-engineering"',
        '"id"\s*:\s*"codebase-design"',
        '"id"\s*:\s*"improve-codebase-architecture"',
        '"id"\s*:\s*"diagnosing-bugs"',
        '"id"\s*:\s*"tdd"',
        '"id"\s*:\s*"code-review"',
        '"id"\s*:\s*"implement"'
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Built executable Skill Pack smoke test failed."
    }
    foreach ($pattern in $requiredSkillPatterns) {
        if ($skillSmoke -notmatch $pattern) {
            throw "Built executable Skill Pack smoke test failed: missing expected engineering Pack/Skill metadata."
        }
    }
    $workerSmoke = (& $executable extensions --self-test 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $workerSmoke -notmatch '"extension_id"\s*:\s*"comfyui"' -or $workerSmoke -notmatch '"skill_engine"\s*:' -or $workerSmoke -notmatch '"id"\s*:\s*"folderbridge-engineering"') {
        throw "Built executable extension worker smoke test failed."
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $executable).Hash.ToLowerInvariant()
    "$hash *FolderBridge.exe" | Set-Content -LiteralPath (Join-Path $releaseDir "FolderBridge.exe.sha256") -Encoding ascii
    Write-Host "Built: $executable"
    Write-Host "Smoke: $smoke"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
