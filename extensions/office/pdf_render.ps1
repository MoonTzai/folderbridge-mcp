[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PdfPath,
    [Parameter(Mandatory = $true)][string]$OutputDir,
    [int]$PageStart = 0,
    [int]$PageEnd = 0,
    [int]$Width = 1920,
    [string]$Overwrite = "false"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

function Write-Result([hashtable]$Payload) {
    $json = $Payload | ConvertTo-Json -Depth 8 -Compress
    [Console]::Out.WriteLine($json)
}

function Normalize-Range([int]$Count, [int]$RequestedStart, [int]$RequestedEnd) {
    if ($Count -lt 1) { throw "The PDF contains no renderable pages." }
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
public static class FolderBridgePdfDpiNative {
    [DllImport("user32.dll")]
    public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiContext);
    [DllImport("user32.dll")]
    public static extern uint GetDpiForSystem();
}
"@

function Get-SystemDpi {
    $systemAware = [IntPtr](-2)
    $previous = [FolderBridgePdfDpiNative]::SetThreadDpiAwarenessContext($systemAware)
    try {
        $dpi = [int][FolderBridgePdfDpiNative]::GetDpiForSystem()
    }
    finally {
        if ($previous -ne [IntPtr]::Zero) {
            [void][FolderBridgePdfDpiNative]::SetThreadDpiAwarenessContext($previous)
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

try {
    $pdfFile = (Resolve-Path -LiteralPath $PdfPath -ErrorAction Stop).Path
    $outputFolder = (Resolve-Path -LiteralPath $OutputDir -ErrorAction Stop).Path
    if (-not (Test-Path -LiteralPath $pdfFile -PathType Leaf)) { throw "PdfPath is not a regular file." }
    if ([IO.Path]::GetExtension($pdfFile).ToLowerInvariant() -ne ".pdf") { throw "PdfPath must be a PDF file." }
    if (-not (Test-Path -LiteralPath $outputFolder -PathType Container)) { throw "OutputDir is not a directory." }
    if ($Width -lt 320 -or $Width -gt 7680) { throw "Width must be between 320 and 7680." }
    $canOverwrite = $Overwrite -eq "true"

    $null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType = WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream, Windows.Storage.Streams, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType = WindowsRuntime]
    $null = [Windows.Data.Pdf.PdfPageRenderOptions, Windows.Data.Pdf, ContentType = WindowsRuntime]

    $storageFile = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pdfFile)) ([Windows.Storage.StorageFile])
    $pdf = Await-Operation ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($storageFile)) ([Windows.Data.Pdf.PdfDocument])
    $pageCount = [int]$pdf.PageCount
    $bounds = Normalize-Range $pageCount $PageStart $PageEnd
    $files = New-Object System.Collections.Generic.List[string]
    $systemDpi = Get-SystemDpi

    for ($pageNumber = $bounds[0]; $pageNumber -le $bounds[1]; $pageNumber++) {
        $page = $null
        $stream = $null
        try {
            $page = $pdf.GetPage([uint32]($pageNumber - 1))
            $size = $page.Size
            $targetHeight = [Math]::Max(1, [int][Math]::Round($Width * $size.Height / $size.Width))
            $widthDips = Convert-PixelsToDips $Width $systemDpi
            $heightDips = Convert-PixelsToDips $targetHeight $systemDpi
            $fileName = "P{0:D4}.png" -f $pageNumber
            $pngPath = Join-Path $outputFolder $fileName
            Ensure-OutputFile $pngPath $canOverwrite
            [IO.File]::WriteAllBytes($pngPath, [byte[]]@())
            $pngStorage = Await-Operation ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pngPath)) ([Windows.Storage.StorageFile])
            $stream = Await-Operation ($pngStorage.OpenAsync([Windows.Storage.FileAccessMode]::ReadWrite)) ([Windows.Storage.Streams.IRandomAccessStream])
            $options = New-Object Windows.Data.Pdf.PdfPageRenderOptions
            $options.DestinationWidth = [uint32]$widthDips
            $options.DestinationHeight = [uint32]$heightDips
            Await-Action ($page.RenderToStreamAsync($stream, $options))
            $stream.Dispose()
            $stream = $null
            Resize-PngToPixels $pngPath $Width $targetHeight
            $files.Add($fileName)
        }
        finally {
            if ($null -ne $stream) { $stream.Dispose() }
            if ($null -ne $page) { $page.Dispose() }
        }
    }

    Write-Result @{
        source_units = $pageCount
        selected_range = @{ start = $bounds[0]; end = $bounds[1]; unit = "page" }
        files = $files.ToArray()
    }
    exit 0
}
catch {
    [Console]::Error.WriteLine(($_ | Out-String).Trim())
    exit 1
}
