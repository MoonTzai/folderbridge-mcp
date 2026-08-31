[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [Parameter(Mandatory = $true)][string]$PidPath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Get-WordProcessIds {
    return @(Get-Process WINWORD -ErrorAction SilentlyContinue | ForEach-Object { [int]$_.Id })
}

function Get-NewWordProcessId([int[]]$BaselinePids) {
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    do {
        $current = @(Get-WordProcessIds)
        $newPids = @($current | Where-Object { $BaselinePids -notcontains $_ })
        if ($newPids.Count -eq 1) {
            return [int]$newPids[0]
        }
        if ($newPids.Count -gt 1) {
            throw "Word automation created an ambiguous set of new WINWORD processes; refusing unsafe ownership."
        }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Could not identify a unique WINWORD process created by this render."
}

function Stop-OwnedWordIfStillRunning([int]$ProcessId) {
    if ($ProcessId -le 0) { return }
    try {
        Wait-Process -Id $ProcessId -Timeout 3 -ErrorAction SilentlyContinue
    }
    catch {}
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        if ($process.ProcessName -ieq "WINWORD") {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
            try { Wait-Process -Id $ProcessId -Timeout 3 -ErrorAction SilentlyContinue } catch {}
        }
    }
    catch {}
}

$app = $null
$doc = $null
$ownedPid = 0
try {
    $source = (Resolve-Path -LiteralPath $InputPath -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "InputPath is not a regular file."
    }
    if ([IO.Path]::GetExtension($source).ToLowerInvariant() -ne ".docx") {
        throw "Word export accepts only .docx input."
    }
    $pdfParent = Split-Path -Parent $PdfPath
    if (-not (Test-Path -LiteralPath $pdfParent -PathType Container)) {
        throw "PDF output parent is not a directory."
    }
    $pidParent = Split-Path -Parent $PidPath
    if (-not (Test-Path -LiteralPath $pidParent -PathType Container)) {
        throw "PID marker parent is not a directory."
    }

    $baselinePids = @(Get-WordProcessIds)
    $app = New-Object -ComObject Word.Application
    $ownedPid = Get-NewWordProcessId $baselinePids
    [IO.File]::WriteAllText($PidPath, [string]$ownedPid, [Text.UTF8Encoding]::new($false))

    $app.Visible = $false
    $app.DisplayAlerts = 0
    $app.AutomationSecurity = 3
    $doc = $app.Documents.Open($source, $false, $true, $false)

    # wdExportFormatPDF=17, wdExportOptimizeForPrint=0,
    # wdExportAllDocument=0, wdExportDocumentContent=0.
    $doc.ExportAsFixedFormat($PdfPath, 17, $false, 0, 0, 1, 1, 0, $true, $true, 0, $true, $true, $false)
    if (-not (Test-Path -LiteralPath $PdfPath -PathType Leaf)) {
        throw "Word did not create the expected PDF output."
    }
    [Console]::Out.WriteLine('{"ok":true}')
    exit 0
}
catch {
    [Console]::Error.WriteLine(($_ | Out-String).Trim())
    exit 1
}
finally {
    if ($null -ne $doc) {
        try { $doc.Close(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($doc) } catch {}
        $doc = $null
    }
    if ($null -ne $app) {
        try { $app.Quit() } catch {}
        try { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app) } catch {}
        $app = $null
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
    Stop-OwnedWordIfStillRunning $ownedPid
}
