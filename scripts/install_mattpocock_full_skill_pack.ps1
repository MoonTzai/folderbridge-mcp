param(
    [string]$PackId = 'matt-pocock-full-additions',
    [switch]$KeepDownload
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# FolderBridge 0.8.21 user Skill Pack root.
$ConfigRoot = if ($env:FOLDERBRIDGE_CONFIG_ROOT) {
    $env:FOLDERBRIDGE_CONFIG_ROOT
} elseif ($env:LOCALAPPDATA) {
    Join-Path $env:LOCALAPPDATA 'folderbridge-mcp'
} else {
    Join-Path $env:USERPROFILE 'AppData\Local\folderbridge-mcp'
}
$SkillRoot = Join-Path $ConfigRoot 'skill-packs'
$Dest = Join-Path $SkillRoot $PackId

$TempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('folderbridge-matt-skills-' + [guid]::NewGuid().ToString('N'))
$ZipPath = Join-Path $TempRoot 'mattpocock-skills-main.zip'
$ExtractRoot = Join-Path $TempRoot 'extract'
$Stage = Join-Path $TempRoot 'pack'

$RepoZip = 'https://github.com/mattpocock/skills/archive/refs/heads/main.zip'
$CommitApi = 'https://api.github.com/repos/mattpocock/skills/commits/main'

# These six are already bundled by FolderBridge Engineering Methods.
$Bundled = @(
    'codebase-design',
    'improve-codebase-architecture',
    'diagnosing-bugs',
    'tdd',
    'code-review',
    'implement'
)
$BundledSet = @{}
foreach ($id in $Bundled) { $BundledSet[$id] = $true }

# ASCII-only routing metadata keeps this installer parse-safe in Windows PowerShell 5.1.
$Routing = @{
    'ask-matt' = @('ask matt','skill router','which skill','workflow routing')
    'grill-with-docs' = @('grill with docs','design interview','ADR','CONTEXT.md','requirements interview')
    'triage' = @('triage','issue triage','bug triage','request triage')
    'setup-matt-pocock-skills' = @('setup matt pocock skills','setup skills','issue tracker setup','MATT setup')
    'to-spec' = @('to spec','specification','implementation spec','write spec')
    'to-tickets' = @('to tickets','implementation tickets','split work','task decomposition')
    'wayfinder' = @('wayfinder','decision map','foggy project','huge feature','path planning')
    'prototype' = @('prototype','throwaway prototype','design question','spike')
    'research' = @('research','primary sources','background research','investigate')
    'domain-modeling' = @('domain modeling','domain model','ubiquitous language','DDD')
    'resolving-merge-conflicts' = @('merge conflict','resolve merge conflicts','git conflict')
    'wizard' = @('wizard','guided workflow','interactive wizard')
    'grill-me' = @('grill me','grill-me','relentless interview','stress test plan')
    'grilling' = @('grilling','grill session','decision interview','ask questions')
    'handoff' = @('handoff','session handoff','new session','context handoff')
    'teach' = @('teach','learning plan','teach concept')
    'to-questionnaire' = @('questionnaire','to questionnaire','questions')
    'wait-what' = @('wait what','clarify','rephrase','too verbose')
    'writing-for-agents' = @('writing for agents','agent instructions','agent writing','prompt docs')
}

function Get-FrontmatterField {
    param([string]$Text, [string]$Field, [string]$Fallback)
    $m = [regex]::Match($Text, '(?ms)\A---\s*\r?\n(?<fm>.*?)\r?\n---')
    if (-not $m.Success) { return $Fallback }
    $f = [regex]::Match($m.Groups['fm'].Value, '(?m)^' + [regex]::Escape($Field) + ':\s*(?<v>.+?)\s*$')
    if (-not $f.Success) { return $Fallback }
    $v = $f.Groups['v'].Value.Trim()
    if (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'"))) {
        $v = $v.Substring(1, $v.Length - 2)
    }
    if ([string]::IsNullOrWhiteSpace($v) -or $v -in @('|','>')) { return $Fallback }
    return $v
}

function Get-FallbackRoutingTerms {
    param([string]$Id, [string]$Description)
    $terms = New-Object System.Collections.Generic.List[string]
    $terms.Add($Id)
    $terms.Add(($Id -replace '-', ' '))
    foreach ($token in ([regex]::Matches($Description.ToLowerInvariant(), '[a-z][a-z0-9-]{3,}'))) {
        if ($terms.Count -ge 12) { break }
        if (-not $terms.Contains($token.Value)) { $terms.Add($token.Value) }
    }
    return $terms.ToArray()
}

function Is-TextResource {
    param([string]$Path)
    $ext = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
    return $ext -in @('.md','.txt','.json','.yaml','.yml','.toml','.ini','.cfg','.csv','.tsv','.js','.mjs','.cjs','.ts','.tsx','.jsx','.py','.ps1','.sh','.bash','.zsh','.fish','.html','.css','.xml')
}

try {
    New-Item -ItemType Directory -Force -Path $TempRoot, $ExtractRoot, $Stage | Out-Null
    Write-Host '[1/7] Downloading official mattpocock/skills main...' -ForegroundColor Cyan
    Invoke-WebRequest -Uri $RepoZip -OutFile $ZipPath -UseBasicParsing

    Write-Host '[2/7] Extracting...' -ForegroundColor Cyan
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractRoot -Force
    $Repo = Get-ChildItem -LiteralPath $ExtractRoot -Directory | Select-Object -First 1
    if (-not $Repo) { throw 'Downloaded archive did not contain a repository root.' }

    $PluginPath = Join-Path $Repo.FullName '.claude-plugin\plugin.json'
    $PackagePath = Join-Path $Repo.FullName 'package.json'
    if (-not (Test-Path -LiteralPath $PluginPath)) { throw 'Official .claude-plugin/plugin.json not found.' }
    $Plugin = Get-Content -LiteralPath $PluginPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $Package = Get-Content -LiteralPath $PackagePath -Raw -Encoding UTF8 | ConvertFrom-Json

    $Commit = ''
    try {
        $headers = @{ 'User-Agent' = 'FolderBridge-MATT-Skill-Pack-Installer' }
        $Commit = (Invoke-RestMethod -Uri $CommitApi -Headers $headers).sha
    } catch {
        Write-Warning ('Could not resolve upstream commit SHA; continuing with version provenance only: ' + $_.Exception.Message)
    }

    Write-Host '[3/7] Resolving promoted upstream skill set...' -ForegroundColor Cyan
    $Promoted = @($Plugin.skills)
    if ($Promoted.Count -lt 1) { throw 'Official plugin manifest contains no promoted skills.' }

    $MissingEntries = New-Object System.Collections.Generic.List[object]
    foreach ($rel in $Promoted) {
        $normalized = ($rel -replace '^\./','') -replace '/', [System.IO.Path]::DirectorySeparatorChar
        $id = Split-Path $normalized -Leaf
        if ($BundledSet.ContainsKey($id)) { continue }
        $MissingEntries.Add([pscustomobject]@{ Id = $id; Relative = $normalized })
    }

    Write-Host ("      Official promoted: {0}; bundled already: {1}; additions to install: {2}" -f $Promoted.Count, $Bundled.Count, $MissingEntries.Count)

    $StageSkills = Join-Path $Stage 'skills'
    New-Item -ItemType Directory -Force -Path $StageSkills | Out-Null
    $ManifestSkills = New-Object System.Collections.Generic.List[object]

    Write-Host '[4/7] Copying missing skill directories and resources...' -ForegroundColor Cyan
    foreach ($entry in $MissingEntries) {
        $src = Join-Path $Repo.FullName $entry.Relative
        if (-not (Test-Path -LiteralPath $src -PathType Container)) { throw "Promoted skill directory missing: $($entry.Relative)" }
        $skillMd = Join-Path $src 'SKILL.md'
        if (-not (Test-Path -LiteralPath $skillMd -PathType Leaf)) { throw "Promoted skill has no SKILL.md: $($entry.Id)" }

        $dst = Join-Path $StageSkills $entry.Id
        Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force

        $skillText = Get-Content -LiteralPath (Join-Path $dst 'SKILL.md') -Raw -Encoding UTF8
        $name = Get-FrontmatterField -Text $skillText -Field 'name' -Fallback $entry.Id
        $description = Get-FrontmatterField -Text $skillText -Field 'description' -Fallback ("Upstream Matt Pocock skill: " + $entry.Id)
        if ($description.Length -gt 1000) { $description = $description.Substring(0,1000) }

        $terms = if ($Routing.ContainsKey($entry.Id)) { @($Routing[$entry.Id]) } else { @(Get-FallbackRoutingTerms -Id $entry.Id -Description $description) }
        if (-not ($terms -contains $entry.Id)) { $terms = @($entry.Id) + $terms }
        $terms = @($terms | Where-Object { $_ -and $_.Length -le 120 } | Select-Object -Unique | Select-Object -First 64)

        $resources = New-Object System.Collections.Generic.List[string]
        Get-ChildItem -LiteralPath $dst -File -Recurse | ForEach-Object {
            if ($_.FullName -eq (Join-Path $dst 'SKILL.md')) { return }
            if ($_.Length -gt 131072) { return }
            if (-not (Is-TextResource -Path $_.FullName)) { return }
            try {
                [void][System.Text.UTF8Encoding]::new($false,$true).GetString([System.IO.File]::ReadAllBytes($_.FullName))
            } catch { return }
            $relativeToStage = $_.FullName.Substring($Stage.Length).TrimStart([char]92,[char]47).Replace([char]92,[char]47)
            $resources.Add($relativeToStage)
        }

        $ManifestSkills.Add([ordered]@{
            id = $entry.Id
            name = $name
            path = ('skills/' + $entry.Id + '/SKILL.md')
            description = $description
            routing_terms = @($terms)
            resources = $resources.ToArray()
        })
    }

    $licenseSrc = Join-Path $Repo.FullName 'LICENSE'
    if (Test-Path -LiteralPath $licenseSrc) { Copy-Item -LiteralPath $licenseSrc -Destination (Join-Path $Stage 'LICENSE.upstream-MIT.txt') -Force }

    $notice = @"
# Matt Pocock full promoted Skill additions for FolderBridge

Generated from the official mattpocock/skills promoted plugin list.
The six methods already bundled in FolderBridge Engineering Methods are intentionally excluded to avoid duplicate routing.
Upstream version: $($Package.version)
Upstream commit: $Commit
Source: mattpocock/skills
License: MIT
"@
    [System.IO.File]::WriteAllText((Join-Path $Stage 'NOTICE.md'), $notice, [System.Text.UTF8Encoding]::new($false))

    $manifest = [ordered]@{
        schema_version = 1
        id = $PackId
        name = 'Matt Pocock Full Skill Additions'
        version = [string]$Package.version
        description = 'Official promoted Matt Pocock skills not already bundled by FolderBridge Engineering Methods. Generated from upstream .claude-plugin/plugin.json.'
        source = [ordered]@{
            repository = 'https://github.com/mattpocock/skills'
            ref = 'main'
            commit = $Commit
            license = 'MIT'
        }
        skills = $ManifestSkills.ToArray()
    }
    $manifestJson = $manifest | ConvertTo-Json -Depth 12
    [System.IO.File]::WriteAllText((Join-Path $Stage 'folderbridge-skill-pack.json'), $manifestJson, [System.Text.UTF8Encoding]::new($false))

    Write-Host '[5/7] Checking FolderBridge pack bounds...' -ForegroundColor Cyan
    $files = @(Get-ChildItem -LiteralPath $Stage -File -Recurse)
    $totalBytes = ($files | Measure-Object Length -Sum).Sum
    if ($files.Count -gt 128) { throw "Pack has $($files.Count) files; FolderBridge limit is 128." }
    if ($totalBytes -gt 4194304) { throw "Pack is $totalBytes bytes; FolderBridge limit is 4 MiB." }
    foreach ($f in $files) {
        if ($f.Name -eq 'folderbridge-skill-pack.json' -and $f.Length -gt 262144) { throw 'Manifest exceeds 256 KiB.' }
    }

    Write-Host '[6/7] Installing to FolderBridge user Skill Pack directory...' -ForegroundColor Cyan
    New-Item -ItemType Directory -Force -Path $SkillRoot | Out-Null
    if (Test-Path -LiteralPath $Dest) {
        $backup = $Dest + '.backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
        Move-Item -LiteralPath $Dest -Destination $backup
        Write-Host "      Existing pack backed up to: $backup"
    }
    Move-Item -LiteralPath $Stage -Destination $Dest

    Write-Host '[7/7] Installed.' -ForegroundColor Green
    Write-Host "      Pack: $Dest"
    Write-Host "      Upstream promoted skills: $($Promoted.Count)"
    Write-Host "      Existing FolderBridge bundled skills skipped: $($Bundled.Count)"
    Write-Host "      Newly installed additions: $($MissingEntries.Count)"
    Write-Host "      Upstream version: $($Package.version)"
    if ($Commit) { Write-Host "      Upstream commit: $Commit" }
    Write-Host ''
    Write-Host 'NEXT: open FolderBridge -> Extensions & Skills -> Skill Packs, rescan if needed, approve the exact hash for this external pack, and enable it.' -ForegroundColor Yellow
    Write-Host 'After approval, a full-chain reload is not normally required; Skill Packs are hot-scanned. Then verify with Skill Engine list.' -ForegroundColor Yellow
}
finally {
    if (-not $KeepDownload -and (Test-Path -LiteralPath $TempRoot)) {
        Remove-Item -LiteralPath $TempRoot -Recurse -Force -ErrorAction SilentlyContinue
    } elseif ($KeepDownload) {
        Write-Host "Temporary download retained at: $TempRoot"
    }
}
