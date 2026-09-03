$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2.0

$Utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
[Console]::InputEncoding = $Utf8Strict
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

$PluginDir = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$VendorDir = [IO.Path]::GetFullPath((Join-Path $PluginDir '_vendor-dotnet'))
$ProvenancePath = [IO.Path]::GetFullPath((Join-Path $PluginDir 'VENDOR-PROVENANCE.json'))
$UnicodeDir = [IO.Path]::GetFullPath((Join-Path $PluginDir 'unicode'))
$CasefoldPath = [IO.Path]::GetFullPath((Join-Path $UnicodeDir 'casefold-map.json'))
$CaseFoldingSourcePath = [IO.Path]::GetFullPath((Join-Path $UnicodeDir 'CaseFolding.txt'))
$UnicodeLicensePath = [IO.Path]::GetFullPath((Join-Path $UnicodeDir 'LICENSE.txt'))
$MaxRequestBytes = 65536
$MinDotNetRelease = 461308
$TOC_MAX_DEPTH = 15
$MaxPageTextChars = 1000000
$MaxReadResponseChars = 500000
$MaxReadPages = 50
$MaxSearchPages = 500
$MaxMetadataChars = 4096
$MaxTocTitleChars = 512
$CasefoldMapSha256 = '77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504'
$CaseFoldingSourceSha256 = 'a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f'
$UnicodeLicenseSha256 = 'e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96'

$ExpectedDlls = @(
    [ordered]@{ file='System.Runtime.CompilerServices.Unsafe.dll'; sha256='08cbd7278b66f1e68425a82d4b97181a4130d93e3dd91831407aba7212ccdacf'; full='System.Runtime.CompilerServices.Unsafe, Version=6.0.3.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a' },
    [ordered]@{ file='System.Buffers.dll'; sha256='2d78d770c9cb997199154ae8c018b9f1d1efbc86729f7264dde6dbad2a12cac3'; full='System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51' },
    [ordered]@{ file='System.Numerics.Vectors.dll'; sha256='20c2fa81b8c70d651099d762954f285fd4f942e63b2d7217c145dab8d4b2f4c9'; full='System.Numerics.Vectors, Version=4.1.6.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a' },
    [ordered]@{ file='System.Memory.dll'; sha256='d5e8e4866f9cfa66f7765660f84b210198893e55335487afe5ebda342c0e913d'; full='System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51' },
    [ordered]@{ file='Microsoft.Bcl.HashCode.dll'; sha256='3a4e851ee5fc0f6182aa5a3d65dc56fcd6979b65334b5c3b92fbdc791457c0ab'; full='Microsoft.Bcl.HashCode, Version=6.0.0.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51' },
    [ordered]@{ file='UglyToad.PdfPig.Core.dll'; sha256='894bf5e8daac5e4f6fbd7e2eb26c6b2f39e42b3122e35fa69c6fa30469a43bb0'; full='UglyToad.PdfPig.Core, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' },
    [ordered]@{ file='UglyToad.PdfPig.DocumentLayoutAnalysis.dll'; sha256='aa79f1774b74e5bd6939e089bb7770fd62b8e8c22d444f5f736a7171c243e16c'; full='UglyToad.PdfPig.DocumentLayoutAnalysis, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' },
    [ordered]@{ file='UglyToad.PdfPig.Fonts.dll'; sha256='b066e7440e7d76d2b8229e9274e300dcfe7dcec65dd578106e8a1bf2473bb911'; full='UglyToad.PdfPig.Fonts, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' },
    [ordered]@{ file='UglyToad.PdfPig.Package.dll'; sha256='ec4a85d737582d93917a4dff811267723092e60f891916dd56dc94630417b5ee'; full='UglyToad.PdfPig.Package, Version=0.1.16.0, Culture=neutral, PublicKeyToken=null' },
    [ordered]@{ file='UglyToad.PdfPig.Tokenization.dll'; sha256='84315ce24887373ed9019442edfcb1b7777e7782ed0c4bf69be63e84941b43e0'; full='UglyToad.PdfPig.Tokenization, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' },
    [ordered]@{ file='UglyToad.PdfPig.Tokens.dll'; sha256='d91a3f93ca27728709875ef71425d4c8e7165d5d3a7b13094ec976cfc22d305c'; full='UglyToad.PdfPig.Tokens, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' },
    [ordered]@{ file='UglyToad.PdfPig.dll'; sha256='cd712f405cbd4400903d18f2855e0b2458acb76d75019765f9faaa2f3ba0717e'; full='UglyToad.PdfPig, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123' }
)

$RedirectMemoryFrom = 'System.Memory, Version=4.0.2.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'
$RedirectMemoryTo = 'System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'
$RedirectBuffersFrom = 'System.Buffers, Version=4.0.4.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'
$RedirectBuffersTo = 'System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'

function Emit-Envelope($Object) {
    [Console]::Out.Write(($Object | ConvertTo-Json -Depth 24 -Compress))
}

function Emit-Error([string]$Code, [string]$Message, $Details) {
    if ($null -eq $Details) { $Details = [ordered]@{} }
    Emit-Envelope ([ordered]@{
        protocol = 1
        ok = $false
        error = [ordered]@{ code=$Code; message=$Message; details=$Details }
    })
}

function Read-Request {
    $Stream = [Console]::OpenStandardInput()
    $Memory = New-Object System.IO.MemoryStream
    $Buffer = New-Object byte[] 8192
    [int]$Total = 0
    while (($Read = $Stream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
        $Total += $Read
        if ($Total -gt $MaxRequestBytes) { throw [System.IO.InvalidDataException]::new('request-too-large') }
        $Memory.Write($Buffer, 0, $Read)
    }
    $Bytes = $Memory.ToArray()
    if ($Bytes.Length -eq 0) { throw [System.IO.InvalidDataException]::new('request-empty') }
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) {
        throw [System.IO.InvalidDataException]::new('request-bom-not-allowed')
    }
    $Text = $Utf8Strict.GetString($Bytes)
    $Object = $Text | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $Object -or $Object -isnot [pscustomobject]) { throw [System.IO.InvalidDataException]::new('request-not-object') }
    return $Object
}

function Require-ExactFields($Object, [string[]]$Allowed) {
    $Names = @($Object.PSObject.Properties.Name)
    foreach ($Name in $Names) {
        if (-not ($Allowed -ccontains [string]$Name)) { throw [System.IO.InvalidDataException]::new('unknown-field:' + [string]$Name) }
    }
    foreach ($Name in $Allowed) {
        if (-not ($Names -ccontains [string]$Name)) { throw [System.IO.InvalidDataException]::new('missing-field:' + [string]$Name) }
    }
}

function Test-Integer($Value) {
    return ($Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or $Value -is [uint16] -or $Value -is [uint32])
}

function Inside-Vendor([string]$Path) {
    $Candidate = [IO.Path]::GetFullPath($Path)
    $Root = $VendorDir.TrimEnd([char]92) + [char]92
    return $Candidate.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)
}

function Read-Provenance {
    if (-not (Test-Path -LiteralPath $ProvenancePath -PathType Leaf)) {
        throw [System.IO.FileNotFoundException]::new('provenance-missing')
    }
    $Info = Get-Item -LiteralPath $ProvenancePath -Force
    if ($Info.Length -gt 1048576) { throw [System.IO.InvalidDataException]::new('provenance-too-large') }
    $Text = $Utf8Strict.GetString([IO.File]::ReadAllBytes($ProvenancePath))
    $Data = $Text | ConvertFrom-Json -ErrorAction Stop
    if ($null -eq $Data -or $Data -isnot [pscustomobject]) { throw [System.IO.InvalidDataException]::new('provenance-not-object') }
    if (-not (Test-Integer $Data.schema_version) -or [int64]$Data.schema_version -ne 3) { throw [System.IO.InvalidDataException]::new('provenance-schema-invalid') }
    if ([string]$Data.extension_version -ne '0.6.0') { throw [System.IO.InvalidDataException]::new('provenance-extension-version-invalid') }
    if ([string]$Data.pdfpig_version -ne '0.1.16') { throw [System.IO.InvalidDataException]::new('provenance-pdfpig-version-invalid') }
    if ([string]$Data.casefold_unicode_version -ne '14.0.0') { throw [System.IO.InvalidDataException]::new('provenance-casefold-version-invalid') }
    if ($null -eq $Data.runtime_dlls -or @($Data.runtime_dlls).Count -ne 12) { throw [System.IO.InvalidDataException]::new('provenance-runtime-dll-count-invalid') }
    return $Data
}

function Verify-And-LoadBackend {
    $Release = 0
    try { $Release = [int](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full' -Name Release -ErrorAction Stop).Release } catch { }
    if ($Release -lt $MinDotNetRelease) { throw [System.PlatformNotSupportedException]::new('dotnet-framework-below-4.7.1') }
    if (-not (Test-Path -LiteralPath $VendorDir -PathType Container)) { throw [System.IO.FileNotFoundException]::new('vendor-directory-missing') }

    $Provenance = Read-Provenance

    if (-not (Test-Path -LiteralPath $UnicodeDir -PathType Container)) { throw [System.IO.FileNotFoundException]::new('casefold-asset-directory-missing') }
    $ActualUnicodeNames = @(Get-ChildItem -LiteralPath $UnicodeDir -File | ForEach-Object { $_.Name } | Sort-Object)
    $ExpectedUnicodeNames = @('CaseFolding.txt','LICENSE.txt','casefold-map.json') | Sort-Object
    if (($ActualUnicodeNames -join '|') -cne ($ExpectedUnicodeNames -join '|')) { throw [System.IO.InvalidDataException]::new('casefold-asset-set-mismatch') }
    foreach ($UnicodeLock in @(
        [ordered]@{ path=$CaseFoldingSourcePath; sha256=$CaseFoldingSourceSha256 },
        [ordered]@{ path=$CasefoldPath; sha256=$CasefoldMapSha256 },
        [ordered]@{ path=$UnicodeLicensePath; sha256=$UnicodeLicenseSha256 }
    )) {
        if (-not (Test-Path -LiteralPath $UnicodeLock.path -PathType Leaf)) { throw [System.IO.FileNotFoundException]::new('casefold-asset-missing') }
        $UnicodeActual = (Get-FileHash -Algorithm SHA256 -LiteralPath $UnicodeLock.path).Hash.ToLowerInvariant()
        if ($UnicodeActual -cne [string]$UnicodeLock.sha256) { throw [System.IO.InvalidDataException]::new('casefold-asset-hash-mismatch') }
    }

    $ProvenanceByFile = @{}
    foreach ($Entry in @($Provenance.runtime_dlls)) {
        $Name = [string]$Entry.file
        if ([string]::IsNullOrEmpty($Name) -or $Name.IndexOfAny(@([char]47,[char]92)) -ge 0) { throw [System.IO.InvalidDataException]::new('provenance-runtime-path-invalid') }
        if ($ProvenanceByFile.ContainsKey($Name)) { throw [System.IO.InvalidDataException]::new('provenance-runtime-duplicate') }
        $ProvenanceByFile[$Name] = $Entry
    }

    $ActualDllNames = @(Get-ChildItem -LiteralPath $VendorDir -File -Filter '*.dll' | ForEach-Object { $_.Name } | Sort-Object)
    $ExpectedDllNames = @($ExpectedDlls | ForEach-Object { [string]$_.file } | Sort-Object)
    if (($ActualDllNames -join '|') -cne ($ExpectedDllNames -join '|')) { throw [System.IO.InvalidDataException]::new('runtime-dll-set-mismatch') }

    # Production loader contract: Assembly.LoadFrom only; no byte-load or simple-name fallback.
    $ApprovedByFullName = @{}
    foreach ($Expected in $ExpectedDlls) {
        $File = [string]$Expected.file
        if (-not $ProvenanceByFile.ContainsKey($File)) { throw [System.IO.InvalidDataException]::new('provenance-runtime-dll-missing:' + $File) }
        $Prov = $ProvenanceByFile[$File]
        if ([string]$Prov.sha256 -cne [string]$Expected.sha256 -or [string]$Prov.full_name -cne [string]$Expected.full) {
            throw [System.IO.InvalidDataException]::new('provenance-runtime-dll-mismatch:' + $File)
        }
        $Path = [IO.Path]::GetFullPath((Join-Path $VendorDir $File))
        if (-not (Inside-Vendor $Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw [System.IO.FileNotFoundException]::new('runtime-dll-missing:' + $File) }
        $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
        if ($Hash -cne [string]$Expected.sha256) { throw [System.IO.InvalidDataException]::new('runtime-dll-hash-mismatch:' + $File) }
        $AssemblyName = [Reflection.AssemblyName]::GetAssemblyName($Path)
        if ($AssemblyName.FullName -cne [string]$Expected.full) { throw [System.IO.InvalidDataException]::new('runtime-dll-identity-mismatch:' + $File) }
        $Simple = [string]$AssemblyName.Name
        foreach ($Loaded in [AppDomain]::CurrentDomain.GetAssemblies()) {
            if ([string]$Loaded.GetName().Name -ceq $Simple) {
                $Location = ''
                try { $Location = [string]$Loaded.Location } catch { }
                if ([string]::IsNullOrEmpty($Location) -or -not (Inside-Vendor $Location)) {
                    throw [System.Security.SecurityException]::new('package-identity-preloaded-outside-vendor:' + $Simple)
                }
            }
        }
        try { $Assembly = [Reflection.Assembly]::LoadFrom($Path) }
        catch { throw [System.IO.FileLoadException]::new('assembly-load-failed:' + $File, $_.Exception) }
        if ($Assembly.FullName -cne [string]$Expected.full) { throw [System.IO.InvalidDataException]::new('loaded-assembly-identity-mismatch:' + $File) }
        $LoadedLocation = ''
        try { $LoadedLocation = [string]$Assembly.Location } catch { }
        if ([string]::IsNullOrEmpty($LoadedLocation) -or -not (Inside-Vendor $LoadedLocation) -or [IO.Path]::GetFullPath($LoadedLocation) -cne $Path) {
            throw [System.Security.SecurityException]::new('loaded-location-outside-vendor:' + $File)
        }
        $ApprovedByFullName[$Assembly.FullName] = $Assembly
    }

    if (-not $ApprovedByFullName.ContainsKey($RedirectMemoryTo) -or -not $ApprovedByFullName.ContainsKey($RedirectBuffersTo)) {
        throw [System.IO.InvalidDataException]::new('redirect-target-missing')
    }
    $MemoryAssembly = $ApprovedByFullName[$RedirectMemoryTo]
    $BuffersAssembly = $ApprovedByFullName[$RedirectBuffersTo]
    $ResolverBlock = {
        param($Sender, $EventArgs)
        $RequestName = [string]$EventArgs.Name
        if ($RequestName -ceq $RedirectMemoryFrom) { return $MemoryAssembly }
        if ($RequestName -ceq $RedirectBuffersFrom) { return $BuffersAssembly }
        return $null
    }.GetNewClosure()
    $Resolver = [System.ResolveEventHandler]$ResolverBlock
    [AppDomain]::CurrentDomain.add_AssemblyResolve($Resolver)

    $PdfType = $null
    $ExtractorType = $null
    foreach ($Assembly in @($ApprovedByFullName.Values)) {
        if ($null -eq $PdfType) { $PdfType = $Assembly.GetType('UglyToad.PdfPig.PdfDocument', $false) }
        if ($null -eq $ExtractorType) { $ExtractorType = $Assembly.GetType('UglyToad.PdfPig.DocumentLayoutAnalysis.TextExtractor.ContentOrderTextExtractor', $false) }
    }
    if ($null -eq $PdfType -or $null -eq $ExtractorType) { throw [System.TypeLoadException]::new('pdfpig-required-types-missing') }
    if (-not (Test-Path -LiteralPath $CasefoldPath -PathType Leaf)) { throw [System.IO.FileNotFoundException]::new('casefold-asset-missing') }

    return [ordered]@{
        release = $Release
        provenance = $Provenance
        assemblies = $ApprovedByFullName
        pdf_type = $PdfType
        extractor_type = $ExtractorType
        resolver = $Resolver
    }
}

function Fail-Domain([string]$Code, [string]$Message) {
    $Failure = New-Object System.Exception($Message)
    $Failure.Data['FBCode'] = $Code
    throw $Failure
}

function Get-DomainCode([Exception]$Exception) {
    $Current = $Exception
    while ($null -ne $Current) {
        if ($Current.Data.Contains('FBCode')) { return [string]$Current.Data['FBCode'] }
        $Current = $Current.InnerException
    }
    return $null
}

function Assert-WellFormed([string]$Text, [string]$ErrorCode) {
    [int]$Index = 0
    while ($Index -lt $Text.Length) {
        [int]$Unit = [char]$Text[$Index]
        if ($Unit -ge 0xD800 -and $Unit -le 0xDBFF) {
            if (($Index + 1) -ge $Text.Length) { Fail-Domain $ErrorCode 'Evidence text contains invalid Unicode.' }
            [int]$Low = [char]$Text[$Index + 1]
            if ($Low -lt 0xDC00 -or $Low -gt 0xDFFF) { Fail-Domain $ErrorCode 'Evidence text contains invalid Unicode.' }
            $Index += 2
            continue
        }
        if ($Unit -ge 0xDC00 -and $Unit -le 0xDFFF) { Fail-Domain $ErrorCode 'Evidence text contains invalid Unicode.' }
        $Index += 1
    }
}

function Measure-Scalars([string]$Text, [string]$ErrorCode) {
    Assert-WellFormed $Text $ErrorCode
    [int]$Index = 0
    [int]$Count = 0
    while ($Index -lt $Text.Length) {
        [int]$Unit = [char]$Text[$Index]
        if ($Unit -ge 0xD800 -and $Unit -le 0xDBFF) { $Index += 2 } else { $Index += 1 }
        $Count += 1
    }
    return $Count
}

function Scalar-ToUtf16Index([string]$Text, [int]$ScalarIndex, [string]$ErrorCode) {
    if ($ScalarIndex -lt 0) { Fail-Domain $ErrorCode 'Negative scalar index.' }
    Assert-WellFormed $Text $ErrorCode
    [int]$Index = 0
    [int]$Count = 0
    while ($Index -lt $Text.Length -and $Count -lt $ScalarIndex) {
        [int]$Unit = [char]$Text[$Index]
        if ($Unit -ge 0xD800 -and $Unit -le 0xDBFF) { $Index += 2 } else { $Index += 1 }
        $Count += 1
    }
    if ($Count -ne $ScalarIndex) { Fail-Domain $ErrorCode 'Scalar index exceeds string length.' }
    return $Index
}

function Take-Scalars([string]$Text, [int]$Maximum, [string]$ErrorCode) {
    if ($Maximum -le 0) { return '' }
    [int]$Total = Measure-Scalars $Text $ErrorCode
    if ($Total -le $Maximum) { return $Text }
    [int]$End = Scalar-ToUtf16Index $Text $Maximum $ErrorCode
    return $Text.Substring(0, $End)
}

function Normalize-ExtractedText([string]$Text) {
    $Normalized = $Text.Replace([string][char]0xFFFE, '-')
    $Normalized = $Normalized.Replace("`r`n", "`n").Replace("`r", "`n")
    return $Normalized
}

function Get-BoundedString($Value, [int]$Maximum, [string]$ErrorCode) {
    $Text = if ($null -eq $Value) { '' } else { [string]$Value }
    [int]$Count = Measure-Scalars $Text $ErrorCode
    return [ordered]@{
        text = $(if ($Count -gt $Maximum) { Take-Scalars $Text $Maximum $ErrorCode } else { $Text })
        truncated = ($Count -gt $Maximum)
    }
}

function Require-StringValue($Value, [string]$Name, [int]$MaximumScalars) {
    if ($Value -isnot [string]) { throw [System.IO.InvalidDataException]::new('field-type-invalid:' + $Name) }
    [int]$Count = Measure-Scalars ([string]$Value) 'PDF_INSPECT_PROTOCOL_ERROR'
    if ($Count -gt $MaximumScalars) { throw [System.IO.InvalidDataException]::new('field-too-long:' + $Name) }
    return [string]$Value
}

function Require-IntegerRange($Value, [string]$Name, [int64]$Minimum, [int64]$Maximum) {
    if (-not (Test-Integer $Value)) { throw [System.IO.InvalidDataException]::new('field-type-invalid:' + $Name) }
    [int64]$Number = [int64]$Value
    if ($Number -lt $Minimum -or $Number -gt $Maximum) { throw [System.IO.InvalidDataException]::new('field-range-invalid:' + $Name) }
    return $Number
}

function Require-BoolValue($Value, [string]$Name) {
    if ($Value -isnot [bool]) { throw [System.IO.InvalidDataException]::new('field-type-invalid:' + $Name) }
    return [bool]$Value
}

function Validate-PdfPath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path.Length -gt 32767) { throw [System.IO.InvalidDataException]::new('path-invalid') }
    if (-not [IO.Path]::IsPathRooted($Path)) { throw [System.IO.InvalidDataException]::new('path-not-rooted') }
    $Full = [IO.Path]::GetFullPath($Path)
    if (-not $Full.EndsWith('.pdf', [StringComparison]::OrdinalIgnoreCase)) { throw [System.IO.InvalidDataException]::new('path-not-pdf') }
    if (-not (Test-Path -LiteralPath $Full -PathType Leaf)) { Fail-Domain 'PDF_OPEN_FAILED' 'PDF file is unavailable.' }
    return $Full
}

function Load-FoldMap {
    if (-not (Test-Path -LiteralPath $CasefoldPath -PathType Leaf)) { throw [System.IO.FileNotFoundException]::new('casefold-asset-missing') }
    $Actual = (Get-FileHash -LiteralPath $CasefoldPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Actual -cne $CasefoldMapSha256) { throw [System.IO.InvalidDataException]::new('casefold-asset-hash-mismatch') }
    $Bytes = [IO.File]::ReadAllBytes($CasefoldPath)
    if ($Bytes.Length -ge 3 -and $Bytes[0] -eq 0xEF -and $Bytes[1] -eq 0xBB -and $Bytes[2] -eq 0xBF) { throw [System.IO.InvalidDataException]::new('casefold-asset-bom-invalid') }
    $Asset = $Utf8Strict.GetString($Bytes) | ConvertFrom-Json -ErrorAction Stop
    if ([string]$Asset.unicode_version -ne '14.0.0') { throw [System.IO.InvalidDataException]::new('casefold-asset-version-mismatch') }
    $Properties = @($Asset.mapping.PSObject.Properties)
    if ($Properties.Count -ne 1530) { throw [System.IO.InvalidDataException]::new('casefold-asset-count-mismatch') }
    $Map = @{}
    foreach ($Property in $Properties) { $Map[[string]$Property.Name] = [string]$Property.Value }
    return $Map
}

function Fold-WithOrigins([string]$Text, $Map, [bool]$NeedOrigins) {
    Assert-WellFormed $Text 'PDF_TEXT_EXTRACT_FAILED'
    $Builder = New-Object System.Text.StringBuilder
    $Origins = if ($NeedOrigins) { New-Object 'System.Collections.Generic.List[int]' } else { $null }
    [int]$Utf16 = 0
    [int]$Origin = 0
    while ($Utf16 -lt $Text.Length) {
        [int]$Unit = [char]$Text[$Utf16]
        if ($Unit -ge 0xD800 -and $Unit -le 0xDBFF) {
            [int]$CodePoint = [char]::ConvertToUtf32($Text[$Utf16], $Text[$Utf16 + 1])
            $Piece = [char]::ConvertFromUtf32($CodePoint)
            $Utf16 += 2
        } else {
            [int]$CodePoint = $Unit
            $Piece = [string][char]$Unit
            $Utf16 += 1
        }
        $Key = $CodePoint.ToString('X', [Globalization.CultureInfo]::InvariantCulture)
        $Mapped = if ($Map.ContainsKey($Key)) { [string]$Map[$Key] } else { [string]$Piece }
        [void]$Builder.Append($Mapped)
        if ($NeedOrigins) {
            for ([int]$MappedIndex = 0; $MappedIndex -lt $Mapped.Length; $MappedIndex++) { [void]$Origins.Add($Origin) }
        }
        $Origin += 1
    }
    return [ordered]@{ text=$Builder.ToString(); origins=$Origins }
}

function Get-Snippet([string]$Text, [int]$StartScalar, [int]$EndScalar, [int]$Width) {
    [int]$Total = Measure-Scalars $Text 'PDF_TEXT_EXTRACT_FAILED'
    [int]$Half = [Math]::Max(20, [Math]::Floor($Width / 2))
    [int]$LeftScalar = [Math]::Max(0, $StartScalar - $Half)
    [int]$RightScalar = [Math]::Min($Total, $EndScalar + $Half)
    [int]$LeftUtf16 = Scalar-ToUtf16Index $Text $LeftScalar 'PDF_TEXT_EXTRACT_FAILED'
    [int]$RightUtf16 = Scalar-ToUtf16Index $Text $RightScalar 'PDF_TEXT_EXTRACT_FAILED'
    $Snippet = $Text.Substring($LeftUtf16, $RightUtf16 - $LeftUtf16).Replace("`n", ' ')
    return ([regex]::Replace($Snippet, '\s+', ' ')).Trim()
}

function Resolve-TextMethod($Backend, $Page) {
    foreach ($Method in @($Backend.extractor_type.GetMethods())) {
        if ($Method.Name -cne 'GetText') { continue }
        $Parameters = $Method.GetParameters()
        if ($Parameters.Count -ne 2 -or $Parameters[1].ParameterType.FullName -cne 'System.Boolean') { continue }
        if ($Parameters[0].ParameterType.IsAssignableFrom($Page.GetType())) { return $Method }
    }
    throw [System.MissingMethodException]::new('content-order-gettext-method-missing')
}

function Get-PageText($Backend, $Document, [int]$PageNumber, [int]$HardCap) {
    try {
        $Page = $Document.GetPage($PageNumber)
        $Method = Resolve-TextMethod $Backend $Page
        $Raw = [string]$Method.Invoke($null, [object[]]@($Page, $false))
        $Text = Normalize-ExtractedText $Raw
        [int]$PageChars = Measure-Scalars $Text 'PDF_TEXT_EXTRACT_FAILED'
        [int]$ExtractedChars = [Math]::Min($PageChars, $HardCap)
        $Extracted = if ($PageChars -gt $HardCap) { Take-Scalars $Text $HardCap 'PDF_TEXT_EXTRACT_FAILED' } else { $Text }
        return [ordered]@{ text=$Extracted; page_chars=$PageChars; extracted_chars=$ExtractedChars; text_truncated=($PageChars -gt $HardCap) }
    } catch {
        $Domain = Get-DomainCode $_.Exception
        if ($null -ne $Domain) { throw }
        Fail-Domain 'PDF_TEXT_EXTRACT_FAILED' ('Could not extract text from page ' + $PageNumber + '.')
    }
}

function Get-PageGeometry($Document, [int]$PageNumber) {
    try {
        $Page = $Document.GetPage($PageNumber)
        $Bounds = $Page.MediaBox.Bounds
        [double]$Width = [double]$Bounds.Width
        [double]$Height = [double]$Bounds.Height
        if ($Width -le 0 -or $Height -le 0 -or [double]::IsNaN($Width) -or [double]::IsNaN($Height) -or [double]::IsInfinity($Width) -or [double]::IsInfinity($Height)) {
            Fail-Domain 'PDF_PAGE_GEOMETRY_FAILED' ('Page ' + $PageNumber + ' has invalid geometry.')
        }
        return [ordered]@{ page=$PageNumber; width_points=[Math]::Round($Width,3); height_points=[Math]::Round($Height,3) }
    } catch {
        $Domain = Get-DomainCode $_.Exception
        if ($null -ne $Domain) { throw }
        Fail-Domain 'PDF_PAGE_GEOMETRY_FAILED' ('Could not read geometry from page ' + $PageNumber + '.')
    }
}

function Open-PdfDocument($Backend, [string]$Path) {
    try {
        $OpenMethods = @($Backend.pdf_type.GetMethods() | Where-Object { $_.Name -ceq 'Open' -and $_.IsStatic } | Sort-Object { $_.GetParameters().Count })
        $Document = $null
        foreach ($Method in $OpenMethods) {
            $Parameters = $Method.GetParameters()
            if ($Parameters.Count -eq 1 -and $Parameters[0].ParameterType.FullName -ceq 'System.String') {
                $Document = $Method.Invoke($null, [object[]]@($Path))
                break
            }
            if ($Parameters.Count -eq 2 -and $Parameters[0].ParameterType.FullName -ceq 'System.String') {
                $Document = $Method.Invoke($null, [object[]]@($Path, $null))
                break
            }
        }
        if ($null -eq $Document) { throw [System.MissingMethodException]::new('pdf-open-method-missing') }
        try { if ([bool]$Document.IsEncrypted) { Fail-Domain 'PDF_PASSWORD_REQUIRED' 'Encrypted/password-protected PDFs are not supported.' } } catch { if ($null -ne (Get-DomainCode $_.Exception)) { throw } }
        return $Document
    } catch {
        $Domain = Get-DomainCode $_.Exception
        if ($null -ne $Domain) { throw }
        $Message = [string]$_.Exception.GetBaseException().Message
        if ($Message -match '(?i)password|encrypted|encryption') { Fail-Domain 'PDF_PASSWORD_REQUIRED' 'Encrypted/password-protected PDFs are not supported.' }
        Fail-Domain 'PDF_OPEN_FAILED' 'Could not open PDF.'
    }
}

function Get-ObjectProperty($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $Property = $Object.PSObject.Properties[$Name]
    if ($null -eq $Property) { return $null }
    return $Property.Value
}

function Get-Metadata($Document) {
    $Information = Get-ObjectProperty $Document 'Information'
    $Fields = [ordered]@{
        title='Title'; author='Author'; subject='Subject'; keywords='Keywords'; creator='Creator'; producer='Producer'; creation_date='CreationDate'; modification_date='ModifiedDate'
    }
    $Result = [ordered]@{}
    $Truncated = New-Object System.Collections.Generic.List[string]
    foreach ($Entry in $Fields.GetEnumerator()) {
        $Value = Get-ObjectProperty $Information ([string]$Entry.Value)
        $Bounded = Get-BoundedString $Value $MaxMetadataChars 'PDF_TEXT_EXTRACT_FAILED'
        $Result[[string]$Entry.Key] = [string]$Bounded.text
        if ([bool]$Bounded.truncated) { [void]$Truncated.Add([string]$Entry.Key) }
    }
    $Version = Get-ObjectProperty $Document 'Version'
    $Format = 'PDF'
    if ($null -ne $Version) {
        if ($Version -is [IFormattable]) { $Format = 'PDF-' + $Version.ToString($null, [Globalization.CultureInfo]::InvariantCulture) }
        else { $Format = 'PDF-' + [string]$Version }
    }
    $Result['format'] = $Format
    $Result['truncated_fields'] = $Truncated.ToArray()
    return $Result
}

function Get-BookmarkPage($Node) {
    foreach ($Candidate in @($Node, (Get-ObjectProperty $Node 'Destination'))) {
        if ($null -eq $Candidate) { continue }
        foreach ($Name in @('PageNumber','Page')) {
            $Value = Get-ObjectProperty $Candidate $Name
            if ($null -ne $Value -and (Test-Integer $Value) -and [int64]$Value -ge 1) { return [int]$Value }
        }
    }
    return $null
}

function Visit-BookmarkNodes($Nodes, [int]$Depth, $State) {
    if ($Depth -gt $TOC_MAX_DEPTH) { [void]$State.reasons.Add('max_depth'); return $true }
    foreach ($Node in @($Nodes)) {
        $State.seen = [int]$State.seen + 1
        if ([int]$State.seen -gt [int]$State.max_items) { [void]$State.reasons.Add('max_items'); return $true }
        $TitleValue = Get-ObjectProperty $Node 'Title'
        $Title = Get-BoundedString $TitleValue $MaxTocTitleChars 'PDF_TEXT_EXTRACT_FAILED'
        [void]$State.items.Add([ordered]@{
            level=$Depth
            title=[string]$Title.text
            title_truncated=[bool]$Title.truncated
            page=(Get-BookmarkPage $Node)
        })
        $Children = Get-ObjectProperty $Node 'Children'
        if ($null -ne $Children -and @($Children).Count -gt 0) {
            if (Visit-BookmarkNodes $Children ($Depth + 1) $State) { return $true }
        }
    }
    return $false
}

function Get-OutlineData($Document, [int]$MaxItems) {
    $State = [ordered]@{
        max_items=$MaxItems
        seen=0
        items=(New-Object System.Collections.Generic.List[object])
        reasons=([System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal))
    }
    $Bookmarks = $null
    try {
        $HasBookmarks = $Document.TryGetBookmarks([ref]$Bookmarks)
        if ($HasBookmarks -and $null -ne $Bookmarks) {
            [void](Visit-BookmarkNodes $Bookmarks.Roots 1 $State)
        }
    } catch {
        $Domain = Get-DomainCode $_.Exception
        if ($null -ne $Domain) { throw }
        Fail-Domain 'PDF_TEXT_EXTRACT_FAILED' 'Could not extract PDF outline.'
    }
    $Truncated = ($State.reasons.Count -gt 0)
    return [ordered]@{
        items=$State.items.ToArray()
        total=$(if ($Truncated) { $null } else { [int]$State.seen })
        seen=[int]$State.seen
        truncated=$Truncated
        reasons=@($State.reasons | Sort-Object)
    }
}

function Get-SamplePages([int]$PageCount, [int]$Count) {
    if ($PageCount -le 0 -or $Count -le 0) { return @() }
    $Count = [Math]::Min($PageCount, $Count)
    if ($Count -eq 1) { return @(1) }
    $Seen = New-Object 'System.Collections.Generic.HashSet[int]'
    $Result = New-Object System.Collections.Generic.List[int]
    for ([int]$Index = 0; $Index -lt $Count; $Index++) {
        [int]$Zero = [int][Math]::Round(($Index * ($PageCount - 1.0)) / ($Count - 1.0), 0, [MidpointRounding]::ToEven)
        [int]$Page = $Zero + 1
        if ($Seen.Add($Page)) { [void]$Result.Add($Page) }
    }
    return @($Result | Sort-Object)
}

function Validate-PageRange([int]$PageCount, [int]$Start, [int]$End, [int]$Maximum, [string]$Purpose) {
    if ($Start -lt 1 -or $End -lt $Start -or $End -gt $PageCount) { Fail-Domain 'PAGE_RANGE_INVALID' ($Purpose + ' page range is invalid.') }
    if (($End - $Start + 1) -gt $Maximum) { Fail-Domain 'PAGE_RANGE_TOO_LARGE' ($Purpose + ' page range exceeds the locked per-call limit.') }
}

function Invoke-InfoAction($Backend, $Document, $Request) {
    [int]$PageCount = [int]$Document.NumberOfPages
    [int]$MaxOutline = [int]$Request.max_outline_items
    [int]$TextSamplePages = [int]$Request.text_sample_pages
    $Outline = Get-OutlineData $Document $MaxOutline
    $SampleNumbers = @(Get-SamplePages $PageCount $TextSamplePages)
    $TextSamples = New-Object System.Collections.Generic.List[object]
    $TextErrors = New-Object System.Collections.Generic.List[object]
    [int]$Textful = 0
    foreach ($PageNumber in $SampleNumbers) {
        try {
            $Sample = Get-PageText $Backend $Document ([int]$PageNumber) 2000
            [void]$TextSamples.Add([ordered]@{
                page=[int]$PageNumber
                page_chars=[int]$Sample.page_chars
                sample_text_chars=[int]$Sample.extracted_chars
                sample_truncated=[bool]$Sample.text_truncated
                error=$false
            })
            if (-not [string]::IsNullOrWhiteSpace([string]$Sample.text)) { $Textful += 1 }
        } catch {
            [void]$TextErrors.Add([ordered]@{ page=[int]$PageNumber; error='PDF_TEXT_EXTRACT_FAILED' })
            [void]$TextSamples.Add([ordered]@{
                page=[int]$PageNumber
                page_chars=$null
                sample_text_chars=$null
                sample_truncated=$null
                error=$true
            })
        }
    }
    [bool]$TextSampleComplete = ($TextErrors.Count -eq 0)
    $ScanCandidate = if (-not $TextSampleComplete) { $null } elseif ($TextSamples.Count -gt 0 -and $Textful -eq 0) { $true } else { $false }
    $ScanNote = if ($ScanCandidate -eq $true) {
        'Heuristic only: sampled pages had no meaningful extractable text.'
    } elseif ($null -eq $ScanCandidate) {
        'Undetermined because one or more sampled text-layer reads failed.'
    } else { $null }
    $PageSizes = New-Object System.Collections.Generic.List[object]
    foreach ($PageNumber in @(Get-SamplePages $PageCount ([Math]::Min(6, $PageCount)))) {
        [void]$PageSizes.Add((Get-PageGeometry $Document ([int]$PageNumber)))
    }
    return [ordered]@{
        action='info'
        page_count=$PageCount
        metadata=(Get-Metadata $Document)
        outline=@($Outline.items)
        outline_total=$Outline.total
        outline_items_seen_at_least=[int]$Outline.seen
        outline_truncated=[bool]$Outline.truncated
        outline_truncation_reasons=@($Outline.reasons)
        outline_max_depth=$TOC_MAX_DEPTH
        sample_page_sizes=$PageSizes.ToArray()
        text_layer_sample=$TextSamples.ToArray()
        text_sample_complete=$TextSampleComplete
        text_sample_errors=$TextErrors.ToArray()
        scan_candidate=$ScanCandidate
        scan_candidate_note=$ScanNote
    }
}

function Invoke-OutlineAction($Document, $Request) {
    [int]$PageCount = [int]$Document.NumberOfPages
    $Outline = Get-OutlineData $Document ([int]$Request.max_items)
    return [ordered]@{
        action='outline'
        page_count=$PageCount
        total_items=$Outline.total
        items_seen_at_least=[int]$Outline.seen
        truncated=[bool]$Outline.truncated
        truncation_reasons=@($Outline.reasons)
        max_depth=$TOC_MAX_DEPTH
        items=@($Outline.items)
    }
}

function Invoke-ReadPagesAction($Backend, $Document, $Request) {
    [int]$PageCount = [int]$Document.NumberOfPages
    [int]$Start = [int]$Request.page_start
    [int]$End = [int]$Request.page_end
    [int]$MaxChars = [int]$Request.max_chars
    Validate-PageRange $PageCount $Start $End $MaxReadPages 'read-pages'
    [int]$Remaining = $MaxChars
    $Pages = New-Object System.Collections.Generic.List[object]
    $TextTruncatedPages = New-Object System.Collections.Generic.List[int]
    [bool]$ResponseTruncated = $false
    $NextPage = $null

    for ([int]$PageNumber = $Start; $PageNumber -le $End; $PageNumber++) {
        $PageData = Get-PageText $Backend $Document $PageNumber $MaxPageTextChars
        if ([bool]$PageData.text_truncated) { [void]$TextTruncatedPages.Add($PageNumber) }
        [string]$Text = [string]$PageData.text
        [int]$TextScalars = [int]$PageData.extracted_chars
        if ($TextScalars -le $Remaining) {
            [void]$Pages.Add([ordered]@{
                page=$PageNumber
                text=$Text
                chars=[int]$PageData.page_chars
                extracted_chars=[int]$PageData.extracted_chars
                text_truncated=[bool]$PageData.text_truncated
                partial=$false
            })
            $Remaining -= $TextScalars
            if ($Remaining -eq 0 -and $PageNumber -lt $End) {
                $ResponseTruncated = $true
                $NextPage = $PageNumber + 1
                break
            }
            continue
        }

        $ResponseTruncated = $true
        if ($PageNumber -eq $Start -and $Pages.Count -eq 0) {
            $PartialText = Take-Scalars $Text $Remaining 'PDF_TEXT_EXTRACT_FAILED'
            [void]$Pages.Add([ordered]@{
                page=$PageNumber
                text=$PartialText
                chars=[int]$PageData.page_chars
                extracted_chars=$Remaining
                text_truncated=[bool]$PageData.text_truncated
                partial=$true
            })
            $NextPage = $null
        } else {
            $NextPage = $PageNumber
        }
        break
    }

    [bool]$CoverageComplete = (-not $ResponseTruncated -and $TextTruncatedPages.Count -eq 0 -and -not (@($Pages | Where-Object { $_.partial }).Count -gt 0))
    return [ordered]@{
        action='read-pages'
        page_count=$PageCount
        page_start=$Start
        page_end=$End
        returned_pages=$Pages.Count
        max_chars=$MaxChars
        response_truncated=$ResponseTruncated
        text_truncated_pages=$TextTruncatedPages.ToArray()
        coverage_complete=$CoverageComplete
        total_truncated=($ResponseTruncated -or $TextTruncatedPages.Count -gt 0)
        next_page=$NextPage
        pages=$Pages.ToArray()
    }
}

function Invoke-SearchAction($Backend, $Document, $Request) {
    [int]$PageCount = [int]$Document.NumberOfPages
    [string]$Query = [string]$Request.query
    [bool]$CaseSensitive = [bool]$Request.case_sensitive
    [int]$MaxResults = [int]$Request.max_results
    [int]$SnippetChars = [int]$Request.snippet_chars
    [int]$Start = [int]$Request.page_start
    [int]$End = if ($null -eq $Request.page_end) { $PageCount } else { [int]$Request.page_end }
    Validate-PageRange $PageCount $Start $End $MaxSearchPages 'search'
    if ([string]::IsNullOrWhiteSpace($Query)) { Fail-Domain 'QUERY_EMPTY' 'query must contain non-whitespace text' }

    $FoldMap = if ($CaseSensitive) { $null } else { Load-FoldMap }
    if ($CaseSensitive) {
        [string]$Needle = $Query
        [int]$NeedleScalars = Measure-Scalars $Query 'PDF_INSPECT_PROTOCOL_ERROR'
    } else {
        $FoldedNeedleData = Fold-WithOrigins $Query $FoldMap $false
        [string]$Needle = [string]$FoldedNeedleData.text
        [int]$NeedleScalars = Measure-Scalars $Query 'PDF_INSPECT_PROTOCOL_ERROR'
    }
    if ($Needle.Length -eq 0) { Fail-Domain 'QUERY_EMPTY' 'query folds to empty text' }

    $Results = New-Object System.Collections.Generic.List[object]
    $TextTruncatedPages = New-Object System.Collections.Generic.List[int]
    [int]$MatchesSeen = 0
    [int]$PagesScanned = 0
    [bool]$WindowComplete = $true

    for ([int]$PageNumber = $Start; $PageNumber -le $End; $PageNumber++) {
        $PageData = Get-PageText $Backend $Document $PageNumber $MaxPageTextChars
        $PagesScanned += 1
        [string]$Text = [string]$PageData.text
        if ([bool]$PageData.text_truncated) { [void]$TextTruncatedPages.Add($PageNumber) }
        if ($CaseSensitive) {
            [string]$Haystack = $Text
            $Origins = $null
        } else {
            $Folded = Fold-WithOrigins $Text $FoldMap $true
            [string]$Haystack = [string]$Folded.text
            $Origins = $Folded.origins
        }
        [int]$Cursor = 0
        [int]$PageMatchIndex = 0
        while ($Cursor -le $Haystack.Length) {
            [int]$Found = $Haystack.IndexOf($Needle, $Cursor, [StringComparison]::Ordinal)
            if ($Found -lt 0) { break }
            $MatchesSeen += 1
            $PageMatchIndex += 1
            if ($MatchesSeen -gt $MaxResults) {
                $WindowComplete = $false
                break
            }
            if ($CaseSensitive) {
                [int]$OriginalStart = Measure-Scalars $Text.Substring(0, $Found) 'PDF_TEXT_EXTRACT_FAILED'
                [int]$OriginalEnd = $OriginalStart + $NeedleScalars
            } else {
                [int]$OriginalStart = [int]$Origins[$Found]
                [int]$LastFolded = [Math]::Min($Origins.Count - 1, $Found + $Needle.Length - 1)
                [int]$OriginalEnd = [int]$Origins[$LastFolded] + 1
            }
            [void]$Results.Add([ordered]@{
                page=$PageNumber
                match_on_page=$PageMatchIndex
                char_offset=$OriginalStart
                char_end=$OriginalEnd
                snippet=(Get-Snippet $Text $OriginalStart $OriginalEnd $SnippetChars)
            })
            $Cursor = $Found + [Math]::Max(1, $Needle.Length)
        }
        if (-not $WindowComplete) { break }
    }

    [bool]$ResultsTruncated = -not $WindowComplete
    return [ordered]@{
        action='search'
        page_count=$PageCount
        query=$Query
        case_sensitive=$CaseSensitive
        page_start=$Start
        page_end=$End
        pages_scanned=$PagesScanned
        results=$Results.ToArray()
        max_results=$MaxResults
        results_truncated=$ResultsTruncated
        truncated=$ResultsTruncated
        matches_total_in_extracted_text=$(if ($WindowComplete) { $MatchesSeen } else { $null })
        matches_seen_at_least=$MatchesSeen
        search_window_complete=$WindowComplete
        text_truncated_pages=$TextTruncatedPages.ToArray()
        text_coverage_complete=($TextTruncatedPages.Count -eq 0)
        coverage_complete=($WindowComplete -and $TextTruncatedPages.Count -eq 0)
        search_mode='literal'
    }
}

function Validate-Request($Request) {
    $InitialNames = @($Request.PSObject.Properties.Name)
    if (-not ($InitialNames -ccontains 'protocol') -or -not ($InitialNames -ccontains 'action')) { throw [System.IO.InvalidDataException]::new('required-fields-missing') }
    if (-not (Test-Integer $Request.protocol) -or [int64]$Request.protocol -ne 1) { throw [System.IO.InvalidDataException]::new('protocol-version-invalid') }
    if ($Request.action -isnot [string]) { throw [System.IO.InvalidDataException]::new('action-type-invalid') }
    [string]$Action = [string]$Request.action
    switch ($Action) {
        'status' {
            Require-ExactFields $Request @('protocol','action')
        }
        'info' {
            Require-ExactFields $Request @('protocol','action','path','max_outline_items','text_sample_pages')
            [void](Require-StringValue $Request.path 'path' 32767)
            [void](Require-IntegerRange $Request.max_outline_items 'max_outline_items' 0 200)
            [void](Require-IntegerRange $Request.text_sample_pages 'text_sample_pages' 0 20)
        }
        'outline' {
            Require-ExactFields $Request @('protocol','action','path','max_items')
            [void](Require-StringValue $Request.path 'path' 32767)
            [void](Require-IntegerRange $Request.max_items 'max_items' 1 500)
        }
        'read-pages' {
            Require-ExactFields $Request @('protocol','action','path','page_start','page_end','max_chars')
            [void](Require-StringValue $Request.path 'path' 32767)
            [int]$Start = Require-IntegerRange $Request.page_start 'page_start' 1 2147483647
            [int]$End = Require-IntegerRange $Request.page_end 'page_end' 1 2147483647
            [void](Require-IntegerRange $Request.max_chars 'max_chars' 1024 $MaxReadResponseChars)
            if ($End -lt $Start) { Fail-Domain 'PAGE_RANGE_INVALID' 'read-pages page range is invalid.' }
            if (($End - $Start + 1) -gt $MaxReadPages) { Fail-Domain 'PAGE_RANGE_TOO_LARGE' 'read-pages page range exceeds the locked per-call limit.' }
        }
        'search' {
            Require-ExactFields $Request @('protocol','action','path','query','case_sensitive','max_results','snippet_chars','page_start','page_end')
            [void](Require-StringValue $Request.path 'path' 32767)
            [string]$Query = Require-StringValue $Request.query 'query' 256
            if ([string]::IsNullOrWhiteSpace($Query)) { Fail-Domain 'QUERY_EMPTY' 'query must contain non-whitespace text' }
            [void](Require-BoolValue $Request.case_sensitive 'case_sensitive')
            [void](Require-IntegerRange $Request.max_results 'max_results' 1 200)
            [void](Require-IntegerRange $Request.snippet_chars 'snippet_chars' 80 2000)
            [int]$Start = Require-IntegerRange $Request.page_start 'page_start' 1 2147483647
            if ($null -ne $Request.page_end) {
                [int]$End = Require-IntegerRange $Request.page_end 'page_end' 1 2147483647
                if ($End -lt $Start) { Fail-Domain 'PAGE_RANGE_INVALID' 'search page range is invalid.' }
                if (($End - $Start + 1) -gt $MaxSearchPages) { Fail-Domain 'PAGE_RANGE_TOO_LARGE' 'search page range exceeds the locked per-call limit.' }
            }
        }
        default { throw [System.IO.InvalidDataException]::new('action-invalid') }
    }
    return $Action
}

function Controlled-BackendError([Exception]$Exception) {
    $Message = [string]$Exception.GetBaseException().Message
    if ($Exception -is [System.PlatformNotSupportedException]) { return @('PDF_BACKEND_UNAVAILABLE','Required Windows PowerShell 5.1/.NET Framework runtime is unavailable.') }
    if ($Message -eq 'provenance-missing') { return @('PDF_VENDOR_PROVENANCE_MISSING','VENDOR-PROVENANCE.json is required in the approved extension tree.') }
    if ($Message -like 'provenance-*') { return @('PDF_VENDOR_PROVENANCE_INVALID','Vendor provenance is malformed or does not match schema v3.') }
    if ($Message -like 'runtime-dll-*' -or $Message -like 'casefold-asset-*' -or $Message -like 'redirect-target-*') { return @('PDF_VENDOR_PROVENANCE_MISMATCH','Vendored runtime assets do not match the approved provenance lock.') }
    if ($Message -like 'package-identity-preloaded-*' -or $Message -like 'loaded-location-*') { return @('PDF_BACKEND_UNTRUSTED','A package-owned assembly was already loaded or resolved outside the approved vendor directory.') }
    if ($Message -like 'loaded-assembly-identity-*') { return @('PDF_BACKEND_VERSION_MISMATCH','A loaded approved-path assembly identity does not match the locked identity.') }
    if ($Message -like 'assembly-load-*' -or $Message -like 'vendor-directory-*' -or $Message -like 'pdfpig-required-types-*') { return @('PDF_BACKEND_UNAVAILABLE','The approved PdfPig backend could not be loaded.') }
    return $null
}

try {
    $Request = Read-Request
    $Action = Validate-Request $Request
    $Backend = Verify-And-LoadBackend

    if ($Action -ceq 'status') {
        $Result = [ordered]@{
            action = 'status'
            inspection_ready = $true
            pdfpig_version = '0.1.16'
            dotnet_release = [int]$Backend.release
            casefold_unicode_version = '14.0.0'
        }
    } else {
        $PdfPath = Validate-PdfPath ([string]$Request.path)
        $Document = Open-PdfDocument $Backend $PdfPath
        try {
            switch ($Action) {
                'info' { $Result = Invoke-InfoAction $Backend $Document $Request }
                'outline' { $Result = Invoke-OutlineAction $Document $Request }
                'read-pages' { $Result = Invoke-ReadPagesAction $Backend $Document $Request }
                'search' { $Result = Invoke-SearchAction $Backend $Document $Request }
                default { throw [System.IO.InvalidDataException]::new('action-dispatch-invalid') }
            }
        } finally {
            if ($null -ne $Document) { $Document.Dispose() }
        }
    }

    Emit-Envelope ([ordered]@{ protocol=1; ok=$true; result=$Result })
    exit 0
} catch {
    $Domain = Get-DomainCode $_.Exception
    if ($null -ne $Domain) {
        $AllowedDomainCodes = @(
            'PAGE_RANGE_INVALID','PAGE_RANGE_TOO_LARGE','QUERY_EMPTY','PDF_OPEN_FAILED','PDF_PASSWORD_REQUIRED',
            'PDF_TEXT_EXTRACT_FAILED','PDF_PAGE_GEOMETRY_FAILED','PDF_INSPECT_PROTOCOL_ERROR'
        )
        if (-not ($AllowedDomainCodes -ccontains $Domain)) {
            [Console]::Error.Write('inspector-crash')
            exit 1
        }
        $DomainMessage = [string]$_.Exception.GetBaseException().Message
        if ($DomainMessage.Length -gt 4096) { $DomainMessage = $DomainMessage.Substring(0,4096) }
        Emit-Error $Domain $DomainMessage ([ordered]@{})
        exit 0
    }

    $Mapped = Controlled-BackendError $_.Exception
    if ($null -ne $Mapped) {
        Emit-Error ([string]$Mapped[0]) ([string]$Mapped[1]) ([ordered]@{})
        exit 0
    }

    $Base = $_.Exception.GetBaseException()
    if ($Base -is [System.IO.InvalidDataException]) {
        Emit-Error 'PDF_INSPECT_PROTOCOL_ERROR' 'Inspection request was invalid.' ([ordered]@{})
        exit 0
    }
    [Console]::Error.Write('inspector-crash')
    exit 1
}
