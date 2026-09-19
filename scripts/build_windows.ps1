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
$publicExternalExtensions = @(
    "blender-toolkit",
    "chatgpt-web-llm-adapter",
    "comfyui",
    "download-toolkit",
    "ffmpeg-toolkit",
    "ftp-toolkit",
    "godot-ai",
    "gpt-sovits-local",
    "pdf-toolkit",
    "storyboard-chatgpt-web",
    "windows-capture-toolkit"
)
$externalReleaseDir = Join-Path $projectRoot "release\external-extensions"
$externalReleaseFileAllowlists = @{
    "chatgpt-web-llm-adapter" = @(
        "folderbridge-extension.json",
        "plugin.py",
        "browser_runtime.py",
        "standalone.py",
        "launch-standalone.ps1",
        "README.md",
        "install.ps1"
    )
    "storyboard-chatgpt-web" = @(
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
    "windows-capture-toolkit" = @(
        "folderbridge-extension.json",
        "plugin.py",
        "README.md",
        "install.ps1"
    )
}
$retiredFileOpsDir = Join-Path $projectRoot "Plugins\extensions\file-ops-toolkit"

# File Ops moved into FolderBridge Core in 0.8.34. A developer checkout can still
# retain an ignored __pycache__ after the tracked plugin source is removed. Clean
# only that exact retired directory, and fail closed if any source/publishable file
# has reappeared instead of silently deleting it.
if (Test-Path -LiteralPath $retiredFileOpsDir -PathType Container) {
    $residualFiles = @(Get-ChildItem -LiteralPath $retiredFileOpsDir -Force -Recurse -File)
    $unexpectedResiduals = @($residualFiles | Where-Object {
        $_.FullName -notmatch '[\\/]__pycache__[\\/]plugin\.cpython-\d+\.pyc$'
    })
    if ($unexpectedResiduals.Count -ne 0) {
        $unexpectedNames = ($unexpectedResiduals | ForEach-Object { $_.FullName }) -join '; '
        throw "Retired File Ops directory contains unexpected files; refusing cleanup: $unexpectedNames"
    }
    Remove-Item -LiteralPath $retiredFileOpsDir -Recurse -Force
    Write-Host "Removed retired File Ops local cache residue: $retiredFileOpsDir"
}

Push-Location $projectRoot
try {
    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Install packaging/requirements-build.txt in an isolated build environment first."
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

    if (Test-Path -LiteralPath $externalReleaseDir) {
        Remove-Item -LiteralPath $externalReleaseDir -Recurse -Force
    }
    New-Item -ItemType Directory -Path $externalReleaseDir -Force | Out-Null
    foreach ($extensionId in $publicExternalExtensions) {
        if ($extensionId -eq "debate-judge-adapter") {
            throw "Private Debate Judge adapter must never enter the public Release allowlist."
        }
        $source = Join-Path $projectRoot "Plugins\extensions\$extensionId"
        $manifestPath = Join-Path $source "folderbridge-extension.json"
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
            throw "Missing public external Extension manifest: $extensionId"
        }
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($manifest.id -ne $extensionId) {
            throw "External Extension manifest id mismatch: expected '$extensionId', got '$($manifest.id)'."
        }
        $version = [string]$manifest.version
        if ($version -notmatch '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$') {
            throw "External Extension '$extensionId' has an unsupported Release version '$version'."
        }
        $assetName = "FolderBridge-Plugin-$extensionId-v$version.zip"
        $assetPath = Join-Path $externalReleaseDir $assetName
        $releaseAllowlist = $null
        if ($externalReleaseFileAllowlists.ContainsKey($extensionId)) {
            $releaseAllowlist = @($externalReleaseFileAllowlists[$extensionId])
        }
        if ($releaseAllowlist) {
            $releaseStage = Join-Path $projectRoot (".build\external-release-" + $extensionId + "-" + [Guid]::NewGuid().ToString("N"))
            try {
                New-Item -ItemType Directory -Path $releaseStage -Force | Out-Null
                foreach ($name in $releaseAllowlist) {
                    $releaseSource = Join-Path $source $name
                    if (-not (Test-Path -LiteralPath $releaseSource -PathType Leaf)) {
                        throw "Missing explicit Release file for '$extensionId': $name"
                    }
                    $releaseDestination = Join-Path $releaseStage $name
                    $releaseParent = Split-Path -Parent $releaseDestination
                    if ($releaseParent) {
                        New-Item -ItemType Directory -Path $releaseParent -Force | Out-Null
                    }
                    Copy-Item -LiteralPath $releaseSource -Destination $releaseDestination
                }
                Compress-Archive -Path (Join-Path $releaseStage "*") -DestinationPath $assetPath -CompressionLevel Optimal -Force
            }
            finally {
                if (Test-Path -LiteralPath $releaseStage) {
                    Remove-Item -LiteralPath $releaseStage -Recurse -Force
                }
            }
        }
        else {
            Compress-Archive -Path (Join-Path $source "*") -DestinationPath $assetPath -CompressionLevel Optimal -Force
        }
        if ($releaseAllowlist) {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive = [IO.Compression.ZipFile]::OpenRead($assetPath)
            try {
                $members = @(
                    $archive.Entries |
                    Where-Object { -not $_.FullName.EndsWith('/') } |
                    ForEach-Object { $_.FullName.Replace('\', '/') } |
                    Sort-Object
                )
                $expectedMembers = @($releaseAllowlist | ForEach-Object { $_.Replace('\', '/') } | Sort-Object)
                $diff = @(Compare-Object -ReferenceObject $expectedMembers -DifferenceObject $members)
                if ($diff.Count -ne 0) {
                    $detail = ($diff | ForEach-Object { "$($_.SideIndicator) $($_.InputObject)" }) -join '; '
                    throw "Explicit Release ZIP member mismatch for '$extensionId': $detail"
                }
            }
            finally {
                $archive.Dispose()
            }
        }
        $assetHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $assetPath).Hash.ToLowerInvariant()
        "$assetHash *$assetName" | Set-Content -LiteralPath ($assetPath + ".sha256") -Encoding ascii
        Write-Host "Packaged external Extension: $assetName"
        Write-Host "SHA-256: $assetHash"
    }

    Write-Host "Built: $executable"
    Write-Host "Smoke: $smoke"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
