param(
    [switch]$Refresh
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$destRoot = Join-Path $repoRoot 'local-private\pdf-toolkit-upstreams'
$git = (Get-Command git.exe -ErrorAction Stop).Source
New-Item -ItemType Directory -Path $destRoot -Force | Out-Null

$repos = @(
    [ordered]@{
        id = 'jztan-pdf-mcp'
        url = 'https://github.com/jztan/pdf-mcp.git'
        license = 'MIT'
        purpose = 'Agent-oriented selective PDF access; bounded info/search/read/render workflow; hidden-text/content-trust ideas; permissive PDFium direction.'
    },
    [ordered]@{
        id = 'aryanbv-pdf-toolkit-mcp'
        url = 'https://github.com/AryanBV/pdf-toolkit-mcp.git'
        license = 'MIT'
        purpose = 'Dependency containment, predictable bounded tools, rendering without a native build toolchain.'
    },
    [ordered]@{
        id = 'espresso3389-pdf-splitter-mcp'
        url = 'https://github.com/espresso3389/pdf-splitter-mcp.git'
        license = 'inspect cloned repository LICENSE before copying any code'
        purpose = 'Compact random-access operation surface: info, page/range read, search, outline, page rendering.'
    },
    [ordered]@{
        id = 'paradyno-pdf-mcp-server'
        url = 'https://github.com/paradyno/pdf-mcp-server.git'
        license = 'repository advertises Apache-2.0; inspect cloned LICENSE before copying code'
        purpose = 'PDFium-oriented architecture, path sandboxing, caching and operation-boundary reference.'
    },
    [ordered]@{
        id = 'nfsarch33-pdf-mcp-server'
        url = 'https://github.com/nfsarch33/pdf-mcp-server.git'
        license = 'research-only: project code currently Apache-2.0, but runtime stack advertises PyMuPDF/AGPL-3.0'
        purpose = 'Research-only negative/feature reference for OCR, forms, table/image extraction and security taxonomy. Do not copy PyMuPDF-dependent implementation into PDF Toolkit.'
    }
)

function Get-DefaultBranch {
    param([string]$Target)
    $head = @(& $git -C $Target ls-remote --symref origin HEAD)
    if ($LASTEXITCODE -ne 0) { throw "Could not query current default branch for $Target" }
    $refLine = $head | Where-Object { $_ -match '^ref:\s+refs/heads/([^\s]+)\s+HEAD$' } | Select-Object -First 1
    if (-not $refLine) { throw "Could not resolve current default branch for $Target" }
    if ($refLine -notmatch '^ref:\s+refs/heads/([^\s]+)\s+HEAD$') { throw "Could not parse current default branch for $Target" }
    return $Matches[1]
}

function Get-CurrentBranch {
    param([string]$Target)
    $branch = (& $git -C $Target branch --show-current).Trim()
    if ($LASTEXITCODE -ne 0) { throw "git branch --show-current failed for $Target" }
    if ($branch) { return $branch }

    $branch = Get-DefaultBranch -Target $Target
    & $git -C $Target fetch --depth 1 origin $branch
    if ($LASTEXITCODE -ne 0) { throw "Could not fetch default branch $branch for detached checkout: $Target" }
    & $git -C $Target checkout -B $branch FETCH_HEAD | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not restore branch $branch for $Target" }
    return $branch
}

$lock = @()
foreach ($repo in $repos) {
    $target = Join-Path $destRoot $repo.id
    if (-not (Test-Path $target)) {
        Write-Host "[clone] $($repo.url) -> $target"
        & $git clone --depth 1 --single-branch -- $repo.url $target
        if ($LASTEXITCODE -ne 0) { throw "git clone failed for $($repo.id)" }
    } elseif ($Refresh) {
        if (-not (Test-Path (Join-Path $target '.git'))) {
            throw "Existing target is not a Git checkout: $target"
        }
        $dirty = (& $git -C $target status --porcelain)
        if ($LASTEXITCODE -ne 0) { throw "git status failed for $($repo.id)" }
        if ($dirty) { throw "Refusing to refresh dirty checkout: $target" }
        $remote = (& $git -C $target remote get-url origin).Trim()
        if ($remote -ne $repo.url) { throw "Origin mismatch for $($repo.id): $remote" }

        $branch = Get-DefaultBranch -Target $target
        Write-Host "[refresh] $($repo.id) $branch"
        & $git -C $target fetch --depth 1 origin $branch
        if ($LASTEXITCODE -ne 0) { throw "git fetch failed for $($repo.id)" }
        & $git -C $target checkout -B $branch FETCH_HEAD | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "git checkout failed for $($repo.id) default branch $branch" }
    } else {
        Write-Host "[keep] $target (use -Refresh for a clean snapshot refresh)"
    }

    $branch = Get-CurrentBranch -Target $target
    $commit = (& $git -C $target rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or -not $commit) { throw "Could not resolve commit for $($repo.id)" }
    $lock += [ordered]@{
        id = $repo.id
        repository = $repo.url
        branch = $branch
        commit = $commit
        license_note = $repo.license
        purpose = $repo.purpose
        local_path = $target
    }
}

$lockPath = Join-Path $destRoot 'UPSTREAMS.lock.json'
$lock | ConvertTo-Json -Depth 5 | Set-Content -Path $lockPath -Encoding UTF8
Write-Host '[ok] Upstream snapshots ready.'
Write-Host "     $lockPath"
