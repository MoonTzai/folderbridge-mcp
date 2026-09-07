[CmdletBinding()]
param(
    [string]$DestinationRoot = "",
    [switch]$SkipBlenderAddon,
    [switch]$SkipEnable
)

$ErrorActionPreference = "Stop"
$extensionId = "blender-toolkit"
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

function Assert-NormalDirectory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Refusing reparse-point $Label directory: $Path"
    }
    if (-not $item.PSIsContainer) {
        throw "$Label exists but is not a directory: $Path"
    }
}

function New-BridgeToken {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([BitConverter]::ToString($bytes)).Replace("-", "")
}

function Find-BlenderExe {
    $cmd = Get-Command blender.exe -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -and (Test-Path -LiteralPath $cmd.Source -PathType Leaf)) {
        return [IO.Path]::GetFullPath($cmd.Source)
    }
    $base = Join-Path $env:ProgramFiles "Blender Foundation"
    if (Test-Path -LiteralPath $base -PathType Container) {
        $items = Get-ChildItem -LiteralPath $base -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending
        foreach ($item in $items) {
            $candidate = Join-Path $item.FullName "blender.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return [IO.Path]::GetFullPath($candidate)
            }
        }
    }
    return $null
}

Assert-NormalDirectory $target "Extension target"

$existingToken = $null
$existingTokenPath = Join-Path $target "bridge-token.txt"
if (Test-Path -LiteralPath $existingTokenPath -PathType Leaf) {
    $candidateToken = (Get-Content -LiteralPath $existingTokenPath -Raw).Trim()
    if ($candidateToken -match '^[0-9A-Fa-f]{64}$') {
        $existingToken = $candidateToken.ToUpperInvariant()
    }
}
$token = if ($existingToken) { $existingToken } else { New-BridgeToken }

$nonce = [Guid]::NewGuid().ToString("N")
$staging = Join-Path $destinationRootPath (".$extensionId.install-" + $nonce)
$backup = Join-Path $destinationRootPath (".$extensionId.backup-" + $nonce)
$backupCreated = $false
$addonBackup = $null

try {
    New-Item -ItemType Directory -Path $staging | Out-Null
    foreach ($name in @("folderbridge-extension.json", "plugin.py", "README.md", "install.ps1")) {
        $source = Join-Path $sourceRoot $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Required Extension file is missing: $name"
        }
        Copy-Item -LiteralPath $source -Destination (Join-Path $staging $name)
    }

    Set-Content -LiteralPath (Join-Path $staging "bridge-token.txt") -Value $token -NoNewline -Encoding ascii

    $testsSource = Join-Path $sourceRoot "tests\test_plugin.py"
    if (Test-Path -LiteralPath $testsSource -PathType Leaf) {
        $testsTarget = Join-Path $staging "tests"
        New-Item -ItemType Directory -Path $testsTarget | Out-Null
        Copy-Item -LiteralPath $testsSource -Destination (Join-Path $testsTarget "test_plugin.py")
    }

    $addonSource = Join-Path $sourceRoot "blender_addon\folderbridge_blender_bridge"
    if (-not $SkipBlenderAddon) {
        if (-not (Test-Path -LiteralPath (Join-Path $addonSource "__init__.py") -PathType Leaf)) {
            throw "Bundled Blender add-on source is missing."
        }
        $blenderExe = Find-BlenderExe
        if (-not $blenderExe) {
            throw "Blender was not found. Install Blender 5.x or pass -SkipBlenderAddon."
        }

        $probe = & $blenderExe --background --factory-startup --python-expr "import bpy; print('__FOLDERBRIDGE_ADDONS__=' + bpy.utils.user_resource('SCRIPTS', path='addons', create=True))" 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Blender failed while locating its user add-on directory:`n$($probe -join [Environment]::NewLine)"
        }
        $marker = $probe | Where-Object { $_ -like "__FOLDERBRIDGE_ADDONS__=*" } | Select-Object -Last 1
        if (-not $marker) {
            throw "Blender did not report its user add-on directory."
        }
        $addonsRoot = ($marker -replace '^__FOLDERBRIDGE_ADDONS__=', '').Trim()
        if (-not $addonsRoot) {
            throw "Blender reported an empty user add-on directory."
        }
        New-Item -ItemType Directory -Path $addonsRoot -Force | Out-Null
        $addonTarget = Join-Path $addonsRoot "folderbridge_blender_bridge"
        Assert-NormalDirectory $addonTarget "Blender add-on target"

        if (Test-Path -LiteralPath $addonTarget) {
            $addonBackup = $addonTarget + ".backup-" + $nonce
            Move-Item -LiteralPath $addonTarget -Destination $addonBackup
        }

        New-Item -ItemType Directory -Path $addonTarget | Out-Null
        Copy-Item -LiteralPath (Join-Path $addonSource "__init__.py") -Destination (Join-Path $addonTarget "__init__.py")
        Set-Content -LiteralPath (Join-Path $addonTarget "bridge-token.txt") -Value $token -NoNewline -Encoding ascii

        $blenderRunning = @(Get-Process -Name blender -ErrorAction SilentlyContinue).Count -gt 0
        if (-not $SkipEnable -and -not $blenderRunning) {
            $enable = & $blenderExe --background --python-expr "import bpy; bpy.ops.preferences.addon_enable(module='folderbridge_blender_bridge'); bpy.ops.wm.save_userpref(); print('__FOLDERBRIDGE_ENABLED__=1')" 2>&1
            if ($LASTEXITCODE -ne 0 -or -not ($enable | Where-Object { $_ -eq "__FOLDERBRIDGE_ENABLED__=1" })) {
                throw "Blender add-on copied but automatic enable failed:`n$($enable -join [Environment]::NewLine)"
            }
        }

        Write-Host "Blender executable: $blenderExe"
        Write-Host "Blender add-on installed: $addonTarget"
        if ($blenderRunning) {
            Write-Host "Blender is currently running. Restart Blender, then ensure Preferences > Add-ons > FolderBridge Blender Bridge is enabled."
        } elseif ($SkipEnable) {
            Write-Host "Automatic enable skipped. Enable 'FolderBridge Blender Bridge' in Blender Preferences > Add-ons."
        } else {
            Write-Host "Blender add-on enabled and user preferences saved."
        }
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
    if ($addonBackup -and (Test-Path -LiteralPath $addonBackup)) {
        Remove-Item -LiteralPath $addonBackup -Recurse -Force
        $addonBackup = $null
    }

    Write-Host ""
    Write-Host "Installed Blender Toolkit external Extension: $target"
    Write-Host "Open FolderBridge > Extensions & Skills, click Rescan, approve the exact hash/permissions, and enable Blender Toolkit."
    Write-Host "The bridge listens only on 127.0.0.1:8766 and requires the install-generated 256-bit token."
}
catch {
    if ((Test-Path -LiteralPath $staging) -and -not (Test-Path -LiteralPath $target)) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
    if ($backupCreated -and (Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
        Move-Item -LiteralPath $backup -Destination $target -ErrorAction SilentlyContinue
        $backupCreated = $false
    }
    if ($addonBackup) {
        $addonTarget = $addonBackup -replace '\.backup-[0-9a-f]+$', ''
        if ((Test-Path -LiteralPath $addonBackup) -and -not (Test-Path -LiteralPath $addonTarget)) {
            Move-Item -LiteralPath $addonBackup -Destination $addonTarget -ErrorAction SilentlyContinue
        }
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
    }
}
