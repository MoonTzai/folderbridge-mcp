[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$InputPath,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [Parameter(Mandatory = $true)][string]$TempDir,
    [int]$PageStart = 0,
    [int]$PageEnd = 0,
    [int]$Width = 1920,
    [string]$SheetsJson = "[]",
    [string]$Overwrite = "false"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Result([hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Depth 12 -Compress
    [Console]::Out.WriteLine($json)
}

function Normalize-Range([int]$Count, [int]$RequestedStart, [int]$RequestedEnd) {
    if ($Count -lt 1) { throw "The Office document contains no renderable units." }
    $start = if ($RequestedStart -gt 0) { $RequestedStart } else { 1 }
    $end = if ($RequestedEnd -gt 0) { $RequestedEnd } else { $Count }
    if ($start -lt 1 -or $end -gt $Count -or $start -gt $end) {
        throw "Requested range $start..$end is outside 1..$Count."
    }
    return @($start, $end)
}

function Ensure-OutputFile([string]$Path, [bool]$CanOverwrite) {
    if ((Test-Path -LiteralPath $Path) -and -not $CanOverwrite) {
        throw "Output already exists: $Path"
    }
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class FolderBridgeOfficePdfDpiNative {
    [DllImport("user32.dll")]
    public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiContext);
    [DllImport("user32.dll")]
    public static extern uint GetDpiForSystem();
}
"@

function Get-SystemDpi {
    $systemAware = [IntPtr](-2)
    $previous = [FolderBridgeOfficePdfDpiNative]::SetThreadDpiAwarenessContext($systemAware)
    try {
        $dpi = [int][FolderBridgeOfficePdfDpiNative]::GetDpiForSystem()
    }
    finally {
        if ($previous -ne [IntPtr]::Zero) {
            [void][FolderBridgeOfficePdfDpiNative]::SetThreadDpiAwarenessContext($previous)
        }
    }
    if ($dpi -lt 96) { return 96 }
    return $dpi
}

function Convert-PixelsToDips([int]$Pixels, [int]$Dpi) {
    return [Math]::Max(1, [int][Math]::Round($Pixels * 96.0 / $Dpi))
}

function Resize-PngToPixels([string]$Path, [int]$TargetWidth, [int]$TargetHeight) {
    Add-Type -AssemblyName System.Drawing
    $image = $null
    $bitmap = $null
    $graphics = $null
    $temporary = "$Path.folderbridge-resize.png"
    try {
        $image = [System.Drawing.Image]::FromFile($Path)
        if ($image.Width -eq $TargetWidth -and $image.Height -eq $TargetHeight) { return }
        $bitmap = New-Object System.Drawing.Bitmap($TargetWidth, $TargetHeight)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
        $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $graphics.DrawImage($image, 0, 0, $TargetWidth, $TargetHeight)
        $graphics.Dispose()
        $graphics = $null
        $bitmap.Save($temporary, [System.Drawing.Imaging.ImageFormat]::Png)
        $bitmap.Dispose()
        $bitmap = $null
        $image.Dispose()
        $image = $null
        Move-Item -LiteralPath $temporary -Destination $Path -Force
    }
    finally {
        if ($null -ne $graphics) { $graphics.Dispose() }
        if ($null -ne $bitmap) { $bitmap.Dispose() }
        if ($null -ne $image) { $image.Dispose() }
        Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue
    }
}

function Get-AwaitMethods {
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $methods = [System.WindowsRuntimeSystemExtensions].GetMethods()
    $generic = $methods | Where-Object {
        $_.Name -eq "AsTask" -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    $action = $methods | Where-Object {
        $_.Name -eq "AsTask" -and -not $_.IsGenericMethod -and $_.GetParameters().Count -eq 1
    } | Select-Object -First 1
    if ($null -eq $generic -or $null -eq $action) {
        throw "Windows Runtime AsTask bridge is unavailable."
    }
    return @($generic, $action)
}

$script:AwaitMethods = $null
function Await-Operation($Operation, [Type]$ResultType) {
    if ($null -eq $script:AwaitMethods) { $script:AwaitMethods = Get-AwaitMethods }
    $method = $script:AwaitMethods[0].MakeGenericMethod($ResultType)
    $task = $method.Invoke($null, @($Operation))
    $task.Wait()
    return $task.Result
}

function Await-Action($Operation) {
    if ($null -eq $script:AwaitMethods) { $script:AwaitMethods = Get-AwaitMethods }
    $task = $script:AwaitMethods[1].Invoke($null, @($Operation))
    $task.Wait()
}

function Render-PdfToPng([string]$PdfPath, [string]$NamePrefix, [int]$RequestedStart, [int]$RequestedEnd, [int]$TargetWidth, [bool]$CanOverwrite) {
    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]

    $storageFile = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($PdfPath)) ([Windows.Storage.StorageFile])
    $pdf = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($storageFile)) ([Windows.Data.Pdf.PdfDocument])
    $pageCount = [int]$pdf.PageCount
    $bounds = Normalize-Range $pageCount $RequestedStart $RequestedEnd
    $files = New-Object System.Collections.Generic.List[string]
    $systemDpi = Get-SystemDpi
    for ($pageNumber = $bounds[0]; $pageNumber -le $bounds[1]; $pageNumber++) {
        $page = $null
        $stream = $null
        try {
            $page = $pdf.GetPage([uint32]($pageNumber - 1))
            $size = $page.Size
            $targetHeight = [Math]::Max(1, [int][Math]::Round($TargetWidth * $size.Height / $size.Width))
            $widthDips = Convert-PixelsToDips $TargetWidth $systemDpi
            $heightDips = Convert-PixelsToDips $targetHeight $systemDpi
            $fileName = if ($NamePrefix -eq "P") { "P{0:D4}.png" -f $pageNumber } else { "{0}-P{1:D4}.png" -f $NamePrefix, $pageNumber }
            $pngPath = Join-Path $OutputDir $fileName
            Ensure-OutputFile $pngPath $CanOverwrite
            [System.IO.File]::WriteAllBytes($pngPath, [byte[]]@())
            $pngStorage = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pngPath)) ([Windows.Storage.StorageFile])
            $stream = Await-Operation ($pngStorage.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $options.DestinationWidth = [uint32]$widthDips
            $options.DestinationHeight = [uint32]$heightDips
            Await-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Dispose()
            $stream = $null
            Resize-PngToPixels $pngPath $TargetWidth $targetHeight
            $files.Add($fileName)
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $page) { $page.Dispose() }
        }
    }
    return @{ Files = $files.ToArray(); PageCount = $pageCount; Start = $bounds[0]; End = $bounds[1] }
}

function Render-PowerPoint([string]$SourcePath, [bool]$CanOverwrite) {
    $app = $null
    $presentation = $null
    try {
        $app = New-Object -ComObject PowerPoint.Application
        $app.AutomationSecurity = 3
        $presentation = $app.Presentations.Open($SourcePath, -1, 0, 0)
        $count = [int]$presentation.Slides.Count
        $bounds = Normalize-Range $count $PageStart $PageEnd
        $slideWidth = [double]$presentation.PageSetup.SlideWidth
        $slideHeight = [double]$presentation.PageSetup.SlideHeight
        $height = [Math]::Max(1, [int][Math]::Round($Width * $slideHeight / $slideWidth))
        $files = New-Object System.Collections.Generic.List[string]
        for ($i = $bounds[0]; $i -le $bounds[1]; $i++) {
            $fileName = "P{0:D4}.png" -f $i
            $outputPath = Join-Path $OutputDir $fileName
            Ensure-OutputFile $outputPath $CanOverwrite
            $slide = $presentation.Slides.Item($i)
            $slide.Export($outputPath, "PNG", $Width, $height)
            $files.Add($fileName)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($slide)
        }
        return @{
            backend = "Microsoft PowerPoint Slide.Export"
            application = "PowerPoint"
            source_units = $count
            selected_range = @{ start = $bounds[0]; end = $bounds[1]; unit = "slide" }
            files = $files.ToArray()
            notes = @("Macros are force-disabled before opening; presentation is opened read-only and without a window.")
        }
    }
    finally {
        if ($null -ne $presentation) {
            try { $presentation.Close() } catch {}
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($presentation)
        }
        if ($null -ne $app) {
            try { $app.Quit() } catch {}
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
        }
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
    }
}

function Safe-SheetPrefix([int]$Index) {
    return "S{0:D3}" -f $Index
}

function Render-Excel([string]$SourcePath, [bool]$CanOverwrite) {
    $app = $null
    $book = $null
    try {
        $app = New-Object -ComObject Excel.Application
        $app.Visible = $false
        $app.DisplayAlerts = $false
        $app.AskToUpdateLinks = $false
        $app.AutomationSecurity = 3
        $book = $app.Workbooks.Open($SourcePath, 0, $true)
        $requestedSheets = @()
        try {
            $decoded = $SheetsJson | ConvertFrom-Json
            if ($null -ne $decoded) { $requestedSheets = @($decoded) }
        }
        catch { throw "sheets must be valid JSON strings" }

        $allNames = New-Object System.Collections.Generic.List[string]
        for ($i = 1; $i -le $book.Worksheets.Count; $i++) {
            $sheetObj = $book.Worksheets.Item($i)
            $allNames.Add([string]$sheetObj.Name)
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sheetObj)
        }
        $targets = New-Object System.Collections.Generic.List[int]
        if ($requestedSheets.Count -eq 0) {
            for ($i = 1; $i -le $allNames.Count; $i++) { $targets.Add($i) }
        }
        else {
            foreach ($name in $requestedSheets) {
                $found = -1
                for ($i = 0; $i -lt $allNames.Count; $i++) {
                    if ($allNames[$i] -ceq [string]$name) { $found = $i + 1; break }
                }
                if ($found -lt 1) { throw "Worksheet not found: $name" }
                if (-not $targets.Contains($found)) { $targets.Add($found) }
            }
        }

        $files = New-Object System.Collections.Generic.List[string]
        $sheetRanges = New-Object System.Collections.Generic.List[object]
        foreach ($index in $targets) {
            $sheet = $null
            try {
                $sheet = $book.Worksheets.Item($index)
                $prefix = Safe-SheetPrefix $index
                $pdfPath = Join-Path $TempDir ("{0}.pdf" -f $prefix)
                # xlTypePDF=0, xlQualityStandard=0; preserve the worksheet print area/layout.
                $sheet.ExportAsFixedFormat(0, $pdfPath, 0, $true, $false)
                $rendered = Render-PdfToPng $pdfPath $prefix $PageStart $PageEnd $Width $CanOverwrite
                foreach ($file in $rendered.Files) { $files.Add($file) }
                $sheetRanges.Add(@{
                    sheet_index = $index
                    sheet_name = [string]$sheet.Name
                    print_pages = $rendered.PageCount
                    rendered_start = $rendered.Start
                    rendered_end = $rendered.End
                })
            }
            finally {
                if ($null -ne $sheet) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($sheet) }
            }
        }
        return @{
            backend = "Microsoft Excel ExportAsFixedFormat + Windows.Data.Pdf"
            application = "Excel"
            source_units = $book.Worksheets.Count
            selected_range = @{ unit = "worksheet-print-page"; sheets = $sheetRanges.ToArray() }
            files = $files.ToArray()
            notes = @("Each selected worksheet is exported with Excel's native print layout, then rasterized by Windows.Data.Pdf.", "page_start/page_end apply independently to each selected worksheet's exported print pages.", "Macros are force-disabled and the workbook is opened read-only with link updates disabled.")
        }
    }
    finally {
        if ($null -ne $book) {
            try { $book.Close($false) } catch {}
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($book)
        }
        if ($null -ne $app) {
            try { $app.Quit() } catch {}
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
        }
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
    }
}

try {
    $inputFile = (Resolve-Path -LiteralPath $InputPath -ErrorAction Stop).Path
    $outputFolder = (Resolve-Path -LiteralPath $OutputDir -ErrorAction Stop).Path
    $tempFolder = (Resolve-Path -LiteralPath $TempDir -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $inputFile -PathType Leaf)) { throw "InputPath is not a regular file." }
    if (-not (Test-Path -LiteralPath $outputFolder -PathType Container)) { throw "OutputDir is not a directory." }
    if (-not (Test-Path -LiteralPath $tempFolder -PathType Container)) { throw "TempDir is not a directory." }
    if ($Width -lt 320 -or $Width -gt 7680) { throw "Width must be between 320 and 7680." }
    $canOverwrite = $Overwrite -eq "true"
    $extension = [IO.Path]::GetExtension($inputFile).ToLowerInvariant()
    switch ($extension) {
        ".pptx" { $result = Render-PowerPoint $inputFile $canOverwrite }
        ".docx" { throw "DOCX rendering is orchestrated by the Office extension worker and is not supported by this shared PowerPoint/Excel stage script." }
        ".xlsx" { $result = Render-Excel $inputFile $canOverwrite }
        default { throw "Unsupported Office format: $extension" }
    }
    Write-Result $result
    exit 0
}
catch {
    [Console]::Error.WriteLine(($_ | Out-String).Trim())
    exit 1
}
