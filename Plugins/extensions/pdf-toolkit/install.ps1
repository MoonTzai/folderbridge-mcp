[CmdletBinding()]
param(
    [string]$DestinationRoot = "$env:LOCALAPPDATA\folderbridge-mcp\extensions",
    [switch]$Force,
    [string]$ReviewedCacheRoot = ""
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-StrictMode -Version 2.0

$ExtensionId = 'pdf-toolkit'
$ExtensionVersion = '0.6.0'
$PdfPigVersion = '0.1.16'
$UnicodeVersion = '14.0.0'
$MaxTreeFiles = 256
$MaxTreeBytes = 67108864
$JournalMaxBytes = 65536
$Utf8Strict = New-Object System.Text.UTF8Encoding($false, $true)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$Invariant = [Globalization.CultureInfo]::InvariantCulture
$MicrosoftCopyright = ([string][char]0x00A9) + ' Microsoft Corporation. All rights reserved.'

$Packages = @(
    [ordered]@{
        id='PdfPig'; version='0.1.16'; selected_tfm='net471'; selected_group='.NETFramework4.7.1'
        url='https://api.nuget.org/v3-flatcontainer/pdfpig/0.1.16/pdfpig.0.1.16.nupkg'
        file='PdfPig.0.1.16.nupkg'
        sha256='d67171846ea8c28f50359137065fec4514266d7a32b23eae6c5f2ebed8ffcfc4'
        sha512='clPAR660u7oGMGO0+I4JAq3olrsbIcKO2m3JtAuOow+IbS62Pyfh00GFnG10Ngl2nizTS5+DsCSlndW6sLvNBQ=='
        license='Apache-2.0'; authors='UglyToad'; copyright=''
        repository_url='https://github.com/UglyToad/PdfPig'; repository_commit='a7bb35662bbbf405efddad50aedc9bcdcf515afc'
        dependencies=@([ordered]@{id='Microsoft.Bcl.HashCode';version='6.0.0'},[ordered]@{id='System.Memory';version='4.6.0'})
    },
    [ordered]@{
        id='Microsoft.Bcl.HashCode'; version='6.0.0'; selected_tfm='net462'; selected_group='.NETFramework4.6.2'
        url='https://api.nuget.org/v3-flatcontainer/microsoft.bcl.hashcode/6.0.0/microsoft.bcl.hashcode.6.0.0.nupkg'
        file='Microsoft.Bcl.HashCode.6.0.0.nupkg'
        sha256='f3b9b2bab0bf8cc717d5fdf6d7aee3ec54e36d9e85bd41347acae161319cbd6b'
        sha512='k0mXL9QMC7IN4nRuAHuLWykH8H/ng04IWmGSj72aqlaHztvyA6NNc90Nipuavy1iqByVwiJVoIDkNH/IlcnfLQ=='
        license='MIT'; authors='Microsoft'; copyright=$MicrosoftCopyright
        repository_url='https://github.com/dotnet/maintenance-packages'; repository_commit='d0c2a5a83211e271826172a6b0510c25a52dbd53'
        dependencies=@()
    },
    [ordered]@{
        id='System.Memory'; version='4.6.3'; selected_tfm='net462'; selected_group='.NETFramework4.6.2'
        url='https://api.nuget.org/v3-flatcontainer/system.memory/4.6.3/system.memory.4.6.3.nupkg'
        file='System.Memory.4.6.3.nupkg'
        sha256='26078aeb758c9ae985e8bf851f973026061da6a5eb4837204d0c2d2204c72955'
        sha512='NXcNYlWoXe5cz9sb8Huo6x2dCZVYkhwKtgE00n/MoI8V4ZI/7/t+EI5bOhQFlZfFjjqM8+U6prjU/aARt7H/tA=='
        license='MIT'; authors='Microsoft'; copyright=$MicrosoftCopyright
        repository_url='https://github.com/dotnet/maintenance-packages'; repository_commit='f62ca0009b038cab4725a720f386623a969d73ad'
        dependencies=@(
            [ordered]@{id='System.Buffers';version='4.6.1'},
            [ordered]@{id='System.Numerics.Vectors';version='4.6.1'},
            [ordered]@{id='System.Runtime.CompilerServices.Unsafe';version='6.1.2'}
        )
    },
    [ordered]@{
        id='System.Buffers'; version='4.6.1'; selected_tfm='net462'; selected_group='.NETFramework4.6.2'
        url='https://api.nuget.org/v3-flatcontainer/system.buffers/4.6.1/system.buffers.4.6.1.nupkg'
        file='System.Buffers.4.6.1.nupkg'
        sha256='b00451e91d016fbec091ad1e361f3a7015e1d91d4047f7e48a74455b2a673d79'
        sha512='qve/dFwECwehSWlZmpkrrlIeATCvo/Hw2koyMrUVcDBy5gXAQrnwX8pHEoqgj8DgkrWuWW1DrQbFqoMbo+Fvrg=='
        license='MIT'; authors='Microsoft'; copyright=$MicrosoftCopyright
        repository_url='https://github.com/dotnet/maintenance-packages'; repository_commit='6b84308c9ad012f53240d72c1d716d7e42546483'
        dependencies=@()
    },
    [ordered]@{
        id='System.Numerics.Vectors'; version='4.6.1'; selected_tfm='net462'; selected_group='.NETFramework4.6.2'
        url='https://api.nuget.org/v3-flatcontainer/system.numerics.vectors/4.6.1/system.numerics.vectors.4.6.1.nupkg'
        file='System.Numerics.Vectors.4.6.1.nupkg'
        sha256='2bc500a86dcb02f2032d6d877f9e2d6e9e4a79080e57239b4198679d4031f2c7'
        sha512='/rkvpUeUPlCY/2qYVQKiUsj5IKaXZcy2+SQAGAfemAdyEF5AgIgYOFNSTMWDXo09JWFX9HB+wV1yCyi2Mwi3TA=='
        license='MIT'; authors='Microsoft'; copyright=$MicrosoftCopyright
        repository_url='https://github.com/dotnet/maintenance-packages'; repository_commit='6b84308c9ad012f53240d72c1d716d7e42546483'
        dependencies=@()
    },
    [ordered]@{
        id='System.Runtime.CompilerServices.Unsafe'; version='6.1.2'; selected_tfm='net462'; selected_group='.NETFramework4.6.2'
        url='https://api.nuget.org/v3-flatcontainer/system.runtime.compilerservices.unsafe/6.1.2/system.runtime.compilerservices.unsafe.6.1.2.nupkg'
        file='System.Runtime.CompilerServices.Unsafe.6.1.2.nupkg'
        sha256='5f6a7f53af3465f92beb6da873ebe0e496206c313313b98badee4355a6b25937'
        sha512='t2aXWJZBkAkRrTOnw31OBELKEVSDD5YvC3O5dXaHFsR66/nRTKm1y3Iq6NwFI5u5IlKrWYfdan66V+GKKkY8hQ=='
        license='MIT'; authors='Microsoft'; copyright=$MicrosoftCopyright
        repository_url='https://github.com/dotnet/maintenance-packages'; repository_commit='f62ca0009b038cab4725a720f386623a969d73ad'
        dependencies=@()
    }
)

$RuntimeDlls = @(
    [ordered]@{package='System.Runtime.CompilerServices.Unsafe';member='lib/net462/System.Runtime.CompilerServices.Unsafe.dll';file='System.Runtime.CompilerServices.Unsafe.dll';sha256='08cbd7278b66f1e68425a82d4b97181a4130d93e3dd91831407aba7212ccdacf';full_name='System.Runtime.CompilerServices.Unsafe, Version=6.0.3.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a'},
    [ordered]@{package='System.Buffers';member='lib/net462/System.Buffers.dll';file='System.Buffers.dll';sha256='2d78d770c9cb997199154ae8c018b9f1d1efbc86729f7264dde6dbad2a12cac3';full_name='System.Buffers, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'},
    [ordered]@{package='System.Numerics.Vectors';member='lib/net462/System.Numerics.Vectors.dll';file='System.Numerics.Vectors.dll';sha256='20c2fa81b8c70d651099d762954f285fd4f942e63b2d7217c145dab8d4b2f4c9';full_name='System.Numerics.Vectors, Version=4.1.6.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a'},
    [ordered]@{package='System.Memory';member='lib/net462/System.Memory.dll';file='System.Memory.dll';sha256='d5e8e4866f9cfa66f7765660f84b210198893e55335487afe5ebda342c0e913d';full_name='System.Memory, Version=4.0.5.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'},
    [ordered]@{package='Microsoft.Bcl.HashCode';member='lib/net462/Microsoft.Bcl.HashCode.dll';file='Microsoft.Bcl.HashCode.dll';sha256='3a4e851ee5fc0f6182aa5a3d65dc56fcd6979b65334b5c3b92fbdc791457c0ab';full_name='Microsoft.Bcl.HashCode, Version=6.0.0.0, Culture=neutral, PublicKeyToken=cc7b13ffcd2ddd51'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.Core.dll';file='UglyToad.PdfPig.Core.dll';sha256='894bf5e8daac5e4f6fbd7e2eb26c6b2f39e42b3122e35fa69c6fa30469a43bb0';full_name='UglyToad.PdfPig.Core, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.DocumentLayoutAnalysis.dll';file='UglyToad.PdfPig.DocumentLayoutAnalysis.dll';sha256='aa79f1774b74e5bd6939e089bb7770fd62b8e8c22d444f5f736a7171c243e16c';full_name='UglyToad.PdfPig.DocumentLayoutAnalysis, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.Fonts.dll';file='UglyToad.PdfPig.Fonts.dll';sha256='b066e7440e7d76d2b8229e9274e300dcfe7dcec65dd578106e8a1bf2473bb911';full_name='UglyToad.PdfPig.Fonts, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.Package.dll';file='UglyToad.PdfPig.Package.dll';sha256='ec4a85d737582d93917a4dff811267723092e60f891916dd56dc94630417b5ee';full_name='UglyToad.PdfPig.Package, Version=0.1.16.0, Culture=neutral, PublicKeyToken=null'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.Tokenization.dll';file='UglyToad.PdfPig.Tokenization.dll';sha256='84315ce24887373ed9019442edfcb1b7777e7782ed0c4bf69be63e84941b43e0';full_name='UglyToad.PdfPig.Tokenization, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.Tokens.dll';file='UglyToad.PdfPig.Tokens.dll';sha256='d91a3f93ca27728709875ef71425d4c8e7165d5d3a7b13094ec976cfc22d305c';full_name='UglyToad.PdfPig.Tokens, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'},
    [ordered]@{package='PdfPig';member='lib/net471/UglyToad.PdfPig.dll';file='UglyToad.PdfPig.dll';sha256='cd712f405cbd4400903d18f2855e0b2458acb76d75019765f9faaa2f3ba0717e';full_name='UglyToad.PdfPig, Version=0.1.16.0, Culture=neutral, PublicKeyToken=605d367334e74123'}
)

$UnicodeSourceUrl = 'https://www.unicode.org/Public/14.0.0/ucd/CaseFolding.txt'
$UnicodeSourceSha256 = 'a566cd48687b2cd897e02501118b2413c14ae86d318f9abbbba97feb84189f0f'
$UnicodeMapSha256 = '77db0452265524962de82ee70bb1d47d2e9539e10ec8ddab2571b252fe0f3504'
$UnicodeLicenseUrl = 'https://www.unicode.org/license.txt'
$UnicodeLicenseSha256 = 'e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96'
$ApacheLicenseUrl = 'https://spdx.org/licenses/Apache-2.0.txt'
$ApacheLicenseSha256 = 'c274f80372d90c012937370f0e1f15087d22e308ef98b27cea5dc0d2d088366c'
$MitLicenseUrl = 'https://spdx.org/licenses/MIT.txt'
$MitLicenseSha256 = 'c3b1b78bc8bd3ea13aa4bc9778442d16560270afa235006d816e5e88cef24db4'

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-Sha512Base64([string]$Path) {
    $sha = [Security.Cryptography.SHA512]::Create()
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        try { $hash = $sha.ComputeHash($stream) } finally { $stream.Dispose() }
    } finally { $sha.Dispose() }
    return [Convert]::ToBase64String($hash)
}

function Get-Sha256Text([string]$Text) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($Utf8NoBom.GetBytes($Text))).Replace('-','').ToLowerInvariant()) }
    finally { $sha.Dispose() }
}

function Assert-RegularDirectory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Container)) { throw "$Label directory is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label directory must not be a reparse point: $Path" }
}

function Assert-RegularFile([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Label file is missing: $Path" }
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "$Label file must not be a reparse point: $Path" }
}

function Ensure-OwnedDirectory([string]$Path, [string]$Label) {
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -ItemType Directory -Path $Path | Out-Null }
    Assert-RegularDirectory $Path $Label
}

function Path-Inside([string]$Child, [string]$Root) {
    $childFull = [IO.Path]::GetFullPath($Child)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([char]92) + [char]92
    return $childFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)
}

function Relative-TreePath([string]$Child, [string]$Root) {
    $childFull = [IO.Path]::GetFullPath($Child)
    $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd([char]92) + [char]92
    if (-not $childFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)) { throw "Path escapes tree root: $Child" }
    return $childFull.Substring($rootFull.Length).Replace([char]92, [char]47)
}

function Remove-ControlTreeBestEffort([string]$Path, [string]$AllowedRoot) {
    if ([string]::IsNullOrEmpty($Path) -or -not (Test-Path -LiteralPath $Path)) { return }
    if (-not (Path-Inside $Path $AllowedRoot)) { throw "Refusing to clean path outside installer control root: $Path" }
    try { Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop } catch { Write-Warning "Could not clean outside-root installer residue: $Path" }
}

function Read-JsonStrictBounded([string]$Path, [int]$MaximumBytes) {
    Assert-RegularFile $Path 'JSON'
    $info = Get-Item -LiteralPath $Path -Force
    if ($info.Length -gt $MaximumBytes) { throw "JSON file exceeds bounded size: $Path" }
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { throw "UTF-8 BOM is not allowed: $Path" }
    $text = $Utf8Strict.GetString($bytes)
    return ($text | ConvertFrom-Json -ErrorAction Stop)
}

function Write-Utf8Atomic([string]$Path, [string]$Text) {
    $parent = Split-Path -Parent $Path
    Assert-RegularDirectory $parent 'atomic-write parent'
    $temp = Join-Path $parent ('.fbpdf-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    $replaceBackup = $null
    $bytes = $Utf8NoBom.GetBytes($Text)
    $stream = [IO.File]::Open($temp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } finally { $stream.Dispose() }
    try {
        if (Test-Path -LiteralPath $Path) {
            # .NET Framework File.Replace requires a legal backup path even when the caller does not retain backups.
            $replaceBackup = Join-Path $parent ('.fbpdf-replace-backup-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            [IO.File]::Replace($temp, $Path, $replaceBackup, $true)
            if (Test-Path -LiteralPath $replaceBackup) { Remove-Item -LiteralPath $replaceBackup -Force }
            $replaceBackup = $null
        } else { [IO.File]::Move($temp, $Path) }
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue }
        # On a failed Replace, retain any backup rather than deleting the last known journal bytes.
        if ($null -ne $replaceBackup -and (Test-Path -LiteralPath $replaceBackup)) {
            Write-Warning "Atomic journal replacement left recovery backup: $replaceBackup"
        }
    }
}

function Acquire-ReviewedFile([string]$CacheRelative, [string]$Url, [string]$ExpectedSha256, [string]$OutputPath) {
    if (-not [string]::IsNullOrWhiteSpace($ReviewedCacheRoot)) {
        $cacheRootFull = [IO.Path]::GetFullPath($ReviewedCacheRoot)
        Assert-RegularDirectory $cacheRootFull 'reviewed cache'
        $sourcePath = [IO.Path]::GetFullPath((Join-Path $cacheRootFull $CacheRelative))
        if (-not (Path-Inside $sourcePath $cacheRootFull)) { throw "Reviewed cache path escaped cache root: $CacheRelative" }
        Assert-RegularFile $sourcePath 'reviewed cache'
        Copy-Item -LiteralPath $sourcePath -Destination $OutputPath
    } else {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $OutputPath
    }
    Assert-RegularFile $OutputPath 'acquired evidence'
    $actual = Get-Sha256 $OutputPath
    if ($actual -cne $ExpectedSha256) { throw "Reviewed acquisition SHA-256 mismatch for $Url. Expected $ExpectedSha256, got $actual" }
}

function Read-Nuspec($Archive) {
    $entries = @($Archive.Entries | Where-Object { $_.FullName -match '^[^/]+\.nuspec$' })
    if ($entries.Count -ne 1) { throw "Locked NuGet package must contain exactly one root nuspec." }
    $settings = New-Object System.Xml.XmlReaderSettings
    $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
    $settings.XmlResolver = $null
    $stream = $entries[0].Open()
    try {
        $reader = [Xml.XmlReader]::Create($stream, $settings)
        try {
            $doc = New-Object System.Xml.XmlDocument
            $doc.XmlResolver = $null
            $doc.Load($reader)
        } finally { $reader.Dispose() }
    } finally { $stream.Dispose() }
    return $doc
}

function Assert-PackageMetadata($Archive, $Package) {
    $doc = Read-Nuspec $Archive
    $metadata = $doc.SelectSingleNode("//*[local-name()='metadata']")
    if ($null -eq $metadata) { throw "NuGet nuspec metadata is missing for $($Package.id)" }
    $idNode = $metadata.SelectSingleNode("*[local-name()='id']")
    $versionNode = $metadata.SelectSingleNode("*[local-name()='version']")
    $authorsNode = $metadata.SelectSingleNode("*[local-name()='authors']")
    $licenseNode = $metadata.SelectSingleNode("*[local-name()='license']")
    if ([string]$idNode.InnerText -cne [string]$Package.id -or [string]$versionNode.InnerText -cne [string]$Package.version) { throw "NuGet identity mismatch for $($Package.id)" }
    if ([string]$authorsNode.InnerText -cne [string]$Package.authors) { throw "NuGet author mismatch for $($Package.id)" }
    if ($null -eq $licenseNode -or [string]$licenseNode.GetAttribute('type') -cne 'expression' -or [string]$licenseNode.InnerText -cne [string]$Package.license) { throw "NuGet license expression mismatch for $($Package.id)" }

    $repoNode = $metadata.SelectSingleNode("*[local-name()='repository']")
    if ($null -eq $repoNode -or [string]$repoNode.GetAttribute('url') -cne [string]$Package.repository_url -or [string]$repoNode.GetAttribute('commit') -cne [string]$Package.repository_commit) { throw "NuGet repository provenance mismatch for $($Package.id)" }

    $groups = @($metadata.SelectNodes("*[local-name()='dependencies']/*[local-name()='group']") | Where-Object { [string]$_.GetAttribute('targetFramework') -ceq [string]$Package.selected_group })
    if ($groups.Count -ne 1) { throw "Selected dependency group missing or ambiguous for $($Package.id)" }
    $actualDeps = @($groups[0].SelectNodes("*[local-name()='dependency']") | ForEach-Object { ([string]$_.GetAttribute('id')) + '=' + ([string]$_.GetAttribute('version')) } | Sort-Object)
    $expectedDeps = @($Package.dependencies | ForEach-Object { ([string]$_.id) + '=' + ([string]$_.version) } | Sort-Object)
    if (($actualDeps -join '|') -cne ($expectedDeps -join '|')) { throw "Selected dependency group mismatch for $($Package.id)" }
}

function Open-LockedPackage([string]$Path, $Package) {
    if ((Get-Sha256 $Path) -cne [string]$Package.sha256) { throw "Locked nupkg SHA-256 mismatch for $($Package.id)" }
    if ((Get-Sha512Base64 $Path) -cne [string]$Package.sha512) { throw "Locked nupkg SHA-512 mismatch for $($Package.id)" }
    $archive = [IO.Compression.ZipFile]::OpenRead($Path)
    try { Assert-PackageMetadata $archive $Package }
    catch { $archive.Dispose(); throw }
    return $archive
}

function Extract-LockedDlls($Archive, [string]$PackageId, [string]$VendorDir) {
    foreach ($dll in @($RuntimeDlls | Where-Object { [string]$_.package -ceq $PackageId })) {
        $matches = @($Archive.Entries | Where-Object { [string]$_.FullName -ceq [string]$dll.member })
        if ($matches.Count -ne 1) { throw "Expected NuGet runtime member missing or ambiguous: $($dll.member)" }
        $target = Join-Path $VendorDir ([string]$dll.file)
        if (Test-Path -LiteralPath $target) { throw "Duplicate runtime DLL output: $target" }
        $input = $matches[0].Open()
        $output = [IO.File]::Open($target, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $input.CopyTo($output); $output.Flush($true) } finally { $output.Dispose(); $input.Dispose() }
        if ((Get-Sha256 $target) -cne [string]$dll.sha256) { throw "Extracted DLL hash mismatch: $($dll.file)" }
        $identity = [Reflection.AssemblyName]::GetAssemblyName($target).FullName
        if ([string]$identity -cne [string]$dll.full_name) { throw "Extracted DLL identity mismatch: $($dll.file)" }
    }
}

function Escape-JsonString([string]$Value) {
    $sb = New-Object Text.StringBuilder
    [void]$sb.Append([char]34)
    for ([int]$i = 0; $i -lt $Value.Length; $i++) {
        [int]$u = [char]$Value[$i]
        switch ($u) {
            8 { [void]$sb.Append('\b'); continue }
            9 { [void]$sb.Append('\t'); continue }
            10 { [void]$sb.Append('\n'); continue }
            12 { [void]$sb.Append('\f'); continue }
            13 { [void]$sb.Append('\r'); continue }
            34 { [void]$sb.Append('\"'); continue }
            92 { [void]$sb.Append('\\'); continue }
        }
        if ($u -lt 32) { [void]$sb.Append('\u' + $u.ToString('x4', $Invariant)); continue }
        if ($u -ge 0xD800 -and $u -le 0xDBFF) {
            if (($i + 1) -ge $Value.Length) { throw 'Unicode generator encountered unpaired high surrogate.' }
            [int]$lo = [char]$Value[$i + 1]
            if ($lo -lt 0xDC00 -or $lo -gt 0xDFFF) { throw 'Unicode generator encountered unpaired high surrogate.' }
            [void]$sb.Append($Value[$i]); [void]$sb.Append($Value[$i + 1]); $i += 1; continue
        }
        if ($u -ge 0xDC00 -and $u -le 0xDFFF) { throw 'Unicode generator encountered unpaired low surrogate.' }
        [void]$sb.Append($Value[$i])
    }
    [void]$sb.Append([char]34)
    return $sb.ToString()
}

function Generate-CasefoldMap([string]$CaseFoldingPath, [string]$OutputPath) {
    $source = $Utf8Strict.GetString([IO.File]::ReadAllBytes($CaseFoldingPath))
    $map = @{}
    foreach ($raw in ($source -split "`n")) {
        $line = (($raw -split '#', 2)[0]).Trim()
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $fields = @($line -split ';' | ForEach-Object { $_.Trim() })
        if ($fields.Count -lt 3 -or @('C','F') -cnotcontains [string]$fields[1]) { continue }
        [int]$cp = [Convert]::ToInt32([string]$fields[0], 16)
        $pieces = New-Object Text.StringBuilder
        foreach ($token in @(([string]$fields[2]) -split '\s+' | Where-Object { $_ })) {
            [void]$pieces.Append([char]::ConvertFromUtf32([Convert]::ToInt32([string]$token, 16)))
        }
        $map[$cp.ToString('X', $Invariant)] = $pieces.ToString()
    }
    if ($map.Count -ne 1530) { throw "Unicode casefold mapping count mismatch: $($map.Count)" }
    [string[]]$keys = @($map.Keys)
    [Array]::Sort($keys, [StringComparer]::Ordinal)
    $sb = New-Object Text.StringBuilder
    [void]$sb.Append('{"mapping":{')
    [bool]$first = $true
    foreach ($key in $keys) {
        if (-not $first) { [void]$sb.Append(',') }
        $first = $false
        [void]$sb.Append((Escape-JsonString $key))
        [void]$sb.Append(':')
        [void]$sb.Append((Escape-JsonString ([string]$map[$key])))
    }
    [void]$sb.Append('},"unicode_version":"14.0.0"}')
    [IO.File]::WriteAllBytes($OutputPath, $Utf8NoBom.GetBytes($sb.ToString()))
    if ((Get-Sha256 $OutputPath) -cne $UnicodeMapSha256) { throw 'Generated Unicode casefold-map.json hash mismatch.' }
}

function Get-TreeInventory([string]$Root) {
    Assert-RegularDirectory $Root 'extension tree'
    $files = New-Object System.Collections.Generic.List[object]
    [int64]$bytes = 0
    foreach ($item in @(Get-ChildItem -LiteralPath $Root -Force -Recurse)) {
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Extension tree contains a reparse point: $($item.FullName)" }
        if ($item.PSIsContainer) { continue }
        $relative = Relative-TreePath $item.FullName $Root
        $bytes += [int64]$item.Length
        [void]$files.Add([ordered]@{path=$relative;bytes=[int64]$item.Length;sha256=(Get-Sha256 $item.FullName)})
    }
    if ($files.Count -gt $MaxTreeFiles -or $bytes -gt $MaxTreeBytes) { throw "Extension tree exceeds host limits: files=$($files.Count), bytes=$bytes" }
    return [ordered]@{files=@($files | Sort-Object path);count=$files.Count;bytes=$bytes}
}

function Expected-RelativeFiles {
    $items = @(
        'NOTICE.md','README.md','folderbridge-extension.json','install.ps1','pdf_inspect.ps1','pdf_render.ps1','plugin.py','VENDOR-PROVENANCE.json',
        'unicode/CaseFolding.txt','unicode/LICENSE.txt','unicode/casefold-map.json',
        'licenses/Apache-2.0.txt','licenses/MIT.txt'
    )
    $items += @($RuntimeDlls | ForEach-Object { '_vendor-dotnet/' + [string]$_.file })
    return @($items | Sort-Object)
}

function Validate-Tree([string]$Root) {
    $inventory = Get-TreeInventory $Root
    $expected = @(Expected-RelativeFiles)
    $actual = @($inventory.files | ForEach-Object { [string]$_.path } | Sort-Object)
    if (($actual -join '|') -cne ($expected -join '|')) { throw "Installed extension inventory mismatch." }

    $manifest = Read-JsonStrictBounded (Join-Path $Root 'folderbridge-extension.json') 1048576
    if ([string]$manifest.id -cne $ExtensionId -or [string]$manifest.version -cne $ExtensionVersion) { throw "Installed manifest identity/version mismatch." }

    $provenancePath = Join-Path $Root 'VENDOR-PROVENANCE.json'
    $provenance = Read-JsonStrictBounded $provenancePath 1048576
    if ([int]$provenance.schema_version -ne 3 -or [string]$provenance.extension_version -cne $ExtensionVersion -or [string]$provenance.pdfpig_version -cne $PdfPigVersion -or [string]$provenance.casefold_unicode_version -cne $UnicodeVersion) { throw "Installed provenance identity mismatch." }

    if (@($provenance.runtime_dlls).Count -ne $RuntimeDlls.Count) { throw "Installed provenance runtime DLL count mismatch." }
    foreach ($expectedDll in $RuntimeDlls) {
        $matches = @($provenance.runtime_dlls | Where-Object { [string]$_.file -ceq [string]$expectedDll.file })
        if ($matches.Count -ne 1) { throw "Installed provenance DLL set mismatch: $($expectedDll.file)" }
        $record = $matches[0]
        if ([string]$record.package -cne [string]$expectedDll.package -or [string]$record.member -cne [string]$expectedDll.member -or [string]$record.sha256 -cne [string]$expectedDll.sha256 -or [string]$record.full_name -cne [string]$expectedDll.full_name) { throw "Installed provenance DLL record mismatch: $($expectedDll.file)" }
        $dllPath = Join-Path (Join-Path $Root '_vendor-dotnet') ([string]$expectedDll.file)
        if ((Get-Sha256 $dllPath) -cne [string]$expectedDll.sha256) { throw "Installed runtime DLL hash mismatch: $($expectedDll.file)" }
        if ([Reflection.AssemblyName]::GetAssemblyName($dllPath).FullName -cne [string]$expectedDll.full_name) { throw "Installed runtime DLL identity mismatch: $($expectedDll.file)" }
    }

    $packageRecords = @($provenance.packages)
    if ($packageRecords.Count -ne $Packages.Count) { throw "Installed provenance package count mismatch." }
    foreach ($pkg in $Packages) {
        $records = @($packageRecords | Where-Object { [string]$_.id -ceq [string]$pkg.id })
        if ($records.Count -ne 1) { throw "Installed provenance package set mismatch: $($pkg.id)" }
        $record = $records[0]
        if ([string]$record.version -cne [string]$pkg.version -or [string]$record.nupkg_sha256 -cne [string]$pkg.sha256 -or [string]$record.selected_tfm -cne [string]$pkg.selected_tfm -or [string]$record.license -cne [string]$pkg.license) { throw "Installed provenance package record mismatch: $($pkg.id)" }
    }

    $lockedData = @(
        @{path='unicode/CaseFolding.txt';sha=$UnicodeSourceSha256},
        @{path='unicode/casefold-map.json';sha=$UnicodeMapSha256},
        @{path='unicode/LICENSE.txt';sha=$UnicodeLicenseSha256},
        @{path='licenses/Apache-2.0.txt';sha=$ApacheLicenseSha256},
        @{path='licenses/MIT.txt';sha=$MitLicenseSha256}
    )
    foreach ($entry in $lockedData) {
        $p = Join-Path $Root ([string]$entry.path)
        if ((Get-Sha256 $p) -cne [string]$entry.sha) { throw "Installed semantic/license asset hash mismatch: $($entry.path)" }
    }

    $expectedContentFiles = @($expected | Where-Object { $_ -cne 'VENDOR-PROVENANCE.json' })
    if (@($provenance.installed_files).Count -ne $expectedContentFiles.Count) { throw "Installed provenance file inventory count mismatch." }
    foreach ($relative in $expectedContentFiles) {
        $records = @($provenance.installed_files | Where-Object { [string]$_.path -ceq $relative })
        if ($records.Count -ne 1) { throw "Installed provenance file inventory mismatch: $relative" }
        $full = Join-Path $Root $relative
        if ([string]$records[0].sha256 -cne (Get-Sha256 $full) -or [int64]$records[0].bytes -ne [int64](Get-Item -LiteralPath $full).Length) { throw "Installed file hash/size mismatch: $relative" }
    }
    return $inventory
}

function Build-Candidate([string]$Staging, [string]$StateDir) {
    Ensure-OwnedDirectory $Staging 'staging'
    $vendor = Join-Path $Staging '_vendor-dotnet'
    $unicode = Join-Path $Staging 'unicode'
    $licenses = Join-Path $Staging 'licenses'
    Ensure-OwnedDirectory $vendor 'vendor'
    Ensure-OwnedDirectory $unicode 'unicode'
    Ensure-OwnedDirectory $licenses 'licenses'

    $source = [IO.Path]::GetFullPath($PSScriptRoot)
    Assert-RegularDirectory $source 'installer source'
    foreach ($name in @('folderbridge-extension.json','plugin.py','pdf_inspect.ps1','pdf_render.ps1','install.ps1','README.md','NOTICE.md')) {
        $from = Join-Path $source $name
        Assert-RegularFile $from 'installer source'
        Copy-Item -LiteralPath $from -Destination (Join-Path $Staging $name)
    }

    $downloads = Join-Path $StateDir 'downloads'
    Ensure-OwnedDirectory $downloads 'download'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    foreach ($pkg in $Packages) {
        $packagePath = Join-Path $downloads ([string]$pkg.file)
        Acquire-ReviewedFile ('downloads/' + [string]$pkg.file) ([string]$pkg.url) ([string]$pkg.sha256) $packagePath
        $archive = Open-LockedPackage $packagePath $pkg
        try { Extract-LockedDlls $archive ([string]$pkg.id) $vendor } finally { $archive.Dispose() }
    }

    $caseFolding = Join-Path $unicode 'CaseFolding.txt'
    Acquire-ReviewedFile 'unicode/CaseFolding.txt' $UnicodeSourceUrl $UnicodeSourceSha256 $caseFolding
    Generate-CasefoldMap $caseFolding (Join-Path $unicode 'casefold-map.json')
    Acquire-ReviewedFile 'unicode/LICENSE.txt' $UnicodeLicenseUrl $UnicodeLicenseSha256 (Join-Path $unicode 'LICENSE.txt')
    Acquire-ReviewedFile 'licenses/Apache-2.0.txt' $ApacheLicenseUrl $ApacheLicenseSha256 (Join-Path $licenses 'Apache-2.0.txt')
    Acquire-ReviewedFile 'licenses/MIT.txt' $MitLicenseUrl $MitLicenseSha256 (Join-Path $licenses 'MIT.txt')

    $manifest = Read-JsonStrictBounded (Join-Path $Staging 'folderbridge-extension.json') 1048576
    if ([string]$manifest.id -cne $ExtensionId -or [string]$manifest.version -cne $ExtensionVersion) { throw 'Staged manifest identity/version mismatch.' }

    $inventoryBeforeProvenance = Get-TreeInventory $Staging
    $installedFiles = @($inventoryBeforeProvenance.files)
    $packageRecords = @()
    foreach ($pkg in $Packages) {
        $packageRecords += [ordered]@{
            id=[string]$pkg.id; version=[string]$pkg.version; source_url=[string]$pkg.url
            nupkg_sha256=[string]$pkg.sha256; official_sha512=[string]$pkg.sha512
            selected_tfm=[string]$pkg.selected_tfm; selected_dependency_group=[string]$pkg.selected_group
            authors=[string]$pkg.authors; copyright=[string]$pkg.copyright; license=[string]$pkg.license
            repository_url=[string]$pkg.repository_url; repository_commit=[string]$pkg.repository_commit
            dependencies=@($pkg.dependencies)
        }
    }
    $runtimeRecords = @()
    foreach ($dll in $RuntimeDlls) {
        $runtimeRecords += [ordered]@{
            package=[string]$dll.package; member=[string]$dll.member; file=[string]$dll.file
            sha256=[string]$dll.sha256; full_name=[string]$dll.full_name
        }
    }
    $provenance = [ordered]@{
        schema_version=3
        extension_id=$ExtensionId
        extension_version=$ExtensionVersion
        pdfpig_version=$PdfPigVersion
        casefold_unicode_version=$UnicodeVersion
        packages=$packageRecords
        runtime_dlls=$runtimeRecords
        unicode=[ordered]@{
            unicode_version=$UnicodeVersion
            source_url=$UnicodeSourceUrl; source_sha256=$UnicodeSourceSha256
            generated_path='unicode/casefold-map.json'; generated_sha256=$UnicodeMapSha256; mapping_count=1530
            license_url=$UnicodeLicenseUrl; license_path='unicode/LICENSE.txt'; license_sha256=$UnicodeLicenseSha256
        }
        licenses=@(
            [ordered]@{expression='Apache-2.0';path='licenses/Apache-2.0.txt';sha256=$ApacheLicenseSha256;packages=@('PdfPig')},
            [ordered]@{expression='MIT';path='licenses/MIT.txt';sha256=$MitLicenseSha256;packages=@('Microsoft.Bcl.HashCode','System.Buffers','System.Memory','System.Numerics.Vectors','System.Runtime.CompilerServices.Unsafe')}
        )
        installed_files=$installedFiles
        host_tree_limits=[ordered]@{max_files=$MaxTreeFiles;max_bytes=$MaxTreeBytes}
    }
    $json = $provenance | ConvertTo-Json -Depth 12
    [IO.File]::WriteAllText((Join-Path $Staging 'VENDOR-PROVENANCE.json'), $json + [Environment]::NewLine, $Utf8NoBom)
    [void](Validate-Tree $Staging)
}

function Validate-JournalShape($Journal, [string]$DestinationKey, [string]$Destination, [string]$StagingRoot, [string]$BackupRoot, [string]$QuarantineRoot) {
    if ($null -eq $Journal -or [int]$Journal.schema_version -ne 1 -or [string]$Journal.destination_key -cne $DestinationKey -or [string]$Journal.destination -cne $Destination) { throw 'INSTALL_RECOVERY_REQUIRED: transaction journal identity mismatch.' }
    if ($Journal.had_previous -isnot [bool]) { throw 'INSTALL_RECOVERY_REQUIRED: transaction journal had_previous is invalid.' }
    if ([string]::IsNullOrWhiteSpace([string]$Journal.transaction_id)) { throw 'INSTALL_RECOVERY_REQUIRED: transaction journal id is invalid.' }
    foreach ($pair in @(
        @([string]$Journal.staging,$StagingRoot,'staging'),
        @([string]$Journal.backup,$BackupRoot,'backup'),
        @([string]$Journal.quarantine,$QuarantineRoot,'quarantine')
    )) {
        if (-not [string]::IsNullOrEmpty([string]$pair[0]) -and -not (Path-Inside ([string]$pair[0]) ([string]$pair[1]))) { throw "INSTALL_RECOVERY_REQUIRED: recorded $($pair[2]) path is outside its control root." }
    }
}

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw 'PDF Toolkit 0.6.0 installer requires Windows.' }

$sourceRoot = [IO.Path]::GetFullPath($PSScriptRoot)
Assert-RegularDirectory $sourceRoot 'installer source'
$destinationRootFull = [IO.Path]::GetFullPath($DestinationRoot)
$destinationParent = Split-Path -Parent $destinationRootFull
if ([string]::IsNullOrWhiteSpace($destinationParent)) { throw 'DestinationRoot must not be a filesystem root.' }
Ensure-OwnedDirectory $destinationParent 'destination parent'
if (Test-Path -LiteralPath $destinationRootFull) { Assert-RegularDirectory $destinationRootFull 'extension hot-scan root' }

$destination = [IO.Path]::GetFullPath((Join-Path $destinationRootFull $ExtensionId))
$destinationIdentity = $destination.TrimEnd([char]92).ToLowerInvariant()
$destinationKey = Get-Sha256Text $destinationIdentity

$lockRoot = Join-Path $destinationParent 'extension-install-locks'
$transactionRoot = Join-Path $destinationParent 'extension-install-transactions'
$stagingRoot = Join-Path $destinationParent 'extension-install-staging'
$backupRoot = Join-Path $destinationParent 'extension-backups'
$quarantineRoot = Join-Path $destinationParent 'extension-quarantine'
foreach ($entry in @(
    @($lockRoot,'lock root'),@($transactionRoot,'transaction root'),@($stagingRoot,'staging root'),
    @($backupRoot,'backup root'),@($quarantineRoot,'quarantine root')
)) { Ensure-OwnedDirectory ([string]$entry[0]) ([string]$entry[1]) }

$lockPath = Join-Path $lockRoot ("pdf-toolkit-$destinationKey.lock")
$lockHandle = $null
try {
    # FileShare.None is the destination-scoped ownership contract.
    $lockHandle = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch {
    throw "INSTALL_BUSY: another PDF Toolkit installer owns destination $destination"
}

$stateDir = Join-Path $transactionRoot ("pdf-toolkit-$destinationKey")
$journalPath = Join-Path $stateDir 'transaction.json'
$journal = $null

function Write-JournalAtomic {
    $text = $script:journal | ConvertTo-Json -Depth 8
    if ($Utf8NoBom.GetByteCount($text) -gt $JournalMaxBytes) { throw 'Transaction journal exceeds bounded size.' }
    Write-Utf8Atomic $journalPath ($text + [Environment]::NewLine)
}

function Set-JournalPhase([string]$Phase) {
    $script:journal['phase'] = $Phase
    Write-JournalAtomic
}

try {
    # Existing recovery state is handled before observing a new had_previous value.
    if (Test-Path -LiteralPath $stateDir) {
        Assert-RegularDirectory $stateDir 'transaction state'
        if (-not (Test-Path -LiteralPath $journalPath -PathType Leaf)) { throw "INSTALL_RECOVERY_REQUIRED: transaction state exists without transaction.json: $stateDir" }
        $previous = Read-JsonStrictBounded $journalPath $JournalMaxBytes
        Validate-JournalShape $previous $destinationKey $destination $stagingRoot $backupRoot $quarantineRoot
        $phase = [string]$previous.phase
        if (@('prepared','old_backed_up','new_published') -ccontains $phase) {
            throw "INSTALL_RECOVERY_REQUIRED: nonterminal transaction phase '$phase'. destination=$destination staging=$($previous.staging) backup=$($previous.backup) quarantine=$($previous.quarantine)"
        }
        if ($phase -ceq 'committed') {
            [void](Validate-Tree $destination)
            Remove-ControlTreeBestEffort ([string]$previous.staging) $stagingRoot
            Remove-ControlTreeBestEffort ([string]$previous.backup) $backupRoot
            Remove-ControlTreeBestEffort ([string]$previous.quarantine) $quarantineRoot
            Remove-Item -LiteralPath $journalPath -Force
            Remove-Item -LiteralPath $stateDir -Recurse -Force
        } elseif ($phase -ceq 'aborted') {
            $priorHadPrevious = [bool]$previous.had_previous
            $liveExists = Test-Path -LiteralPath $destination
            $backupExists = (-not [string]::IsNullOrEmpty([string]$previous.backup)) -and (Test-Path -LiteralPath ([string]$previous.backup))
            if (($priorHadPrevious -and (-not $liveExists -or $backupExists)) -or ((-not $priorHadPrevious) -and $liveExists)) {
                throw "INSTALL_RECOVERY_REQUIRED: aborted transaction topology no longer matches its initial state."
            }
            Remove-ControlTreeBestEffort ([string]$previous.staging) $stagingRoot
            Remove-ControlTreeBestEffort ([string]$previous.quarantine) $quarantineRoot
            Remove-Item -LiteralPath $journalPath -Force
            Remove-Item -LiteralPath $stateDir -Recurse -Force
        } else {
            throw "INSTALL_RECOVERY_REQUIRED: unknown transaction phase '$phase'."
        }
    }

    $hadPrevious = Test-Path -LiteralPath $destination
    if ($hadPrevious) { Assert-RegularDirectory $destination 'live extension' }

    $transactionId = [Guid]::NewGuid().ToString('N')
    $staging = Join-Path $stagingRoot ("pdf-toolkit-$destinationKey-$transactionId")
    $backup = if ($hadPrevious -and $Force) { Join-Path $backupRoot ("pdf-toolkit-$destinationKey-$transactionId") } else { '' }
    $quarantine = Join-Path $quarantineRoot ("pdf-toolkit-$destinationKey-$transactionId")
    New-Item -ItemType Directory -Path $stateDir | Out-Null
    Assert-RegularDirectory $stateDir 'transaction state'
    $journal = [ordered]@{
        schema_version=1; destination_key=$destinationKey; destination=$destination; transaction_id=$transactionId
        had_previous=[bool]$hadPrevious; phase='prepared'; staging=$staging; backup=$backup; quarantine=$quarantine
    }
    Write-JournalAtomic

    if ($hadPrevious -and -not $Force) {
        Set-JournalPhase 'aborted'
        Remove-Item -LiteralPath $journalPath -Force
        Remove-Item -LiteralPath $stateDir -Recurse -Force
        throw "Destination already exists: $destination. Re-run with -Force only after reviewing the replacement."
    }

    New-Item -ItemType Directory -Path $staging | Out-Null
    try {
        Build-Candidate $staging $stateDir
    } catch {
        $buildError = $_
        if (($hadPrevious -and (Test-Path -LiteralPath $destination) -and [string]::IsNullOrEmpty($backup)) -or ((-not $hadPrevious) -and -not (Test-Path -LiteralPath $destination))) {
            Set-JournalPhase 'aborted'
            Remove-ControlTreeBestEffort $staging $stagingRoot
            Remove-Item -LiteralPath $journalPath -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $stateDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        throw $buildError
    }

    try {
        if ($hadPrevious) {
            if (Test-Path -LiteralPath $backup) { throw "Backup destination already exists: $backup" }
            Move-Item -LiteralPath $destination -Destination $backup
            Set-JournalPhase 'old_backed_up'
        }

        if (-not (Test-Path -LiteralPath $destinationRootFull)) {
            New-Item -ItemType Directory -Path $destinationRootFull | Out-Null
            Assert-RegularDirectory $destinationRootFull 'extension hot-scan root'
        }
        Move-Item -LiteralPath $staging -Destination $destination
        Set-JournalPhase 'new_published'
        [void](Validate-Tree $destination)
        Set-JournalPhase 'committed'

        if ($hadPrevious -and (Test-Path -LiteralPath $backup)) { Remove-ControlTreeBestEffort $backup $backupRoot }
        Remove-ControlTreeBestEffort $quarantine $quarantineRoot
        Remove-Item -LiteralPath $journalPath -Force
        Remove-Item -LiteralPath $stateDir -Recurse -Force

        Write-Host '[ok] PDF Toolkit 0.6.0 filesystem publish completed.'
        Write-Host "     $destination"
        Write-Host 'Trust is NOT carried over from v0.5.x.'
        Write-Host 'Next: FolderBridge -> Extensions & Skills -> rescan -> review the newly computed exact tree hash + permissions -> approve -> enable.'
        Write-Host 'Only after exact-hash reapproval should runtime acceptance begin.'
    } catch {
        $installError = $_
        try {
            if (Test-Path -LiteralPath $destination) {
                if (Test-Path -LiteralPath $quarantine) { throw "Quarantine destination already exists: $quarantine" }
                Move-Item -LiteralPath $destination -Destination $quarantine
            }
        } catch {
            throw "INSTALL_RECOVERY_REQUIRED: failed-new live tree could not be moved to quarantine. Preserve recovery state. destination=$destination backup=$backup quarantine=$quarantine error=$($_.Exception.Message)"
        }

        if ($hadPrevious) {
            if (-not (Test-Path -LiteralPath $backup)) {
                throw "INSTALL_RECOVERY_REQUIRED: previous live backup is missing after failed publish. destination=$destination quarantine=$quarantine"
            }
            try {
                Move-Item -LiteralPath $backup -Destination $destination
            } catch {
                throw "INSTALL_RECOVERY_REQUIRED: old tree restore failed. Preserve backup and quarantine. backup=$backup quarantine=$quarantine error=$($_.Exception.Message)"
            }
        }

        $safeInitial = if ($hadPrevious) {
            (Test-Path -LiteralPath $destination) -and (-not (Test-Path -LiteralPath $backup))
        } else {
            -not (Test-Path -LiteralPath $destination)
        }
        if (-not $safeInitial) { throw "INSTALL_RECOVERY_REQUIRED: rollback did not restore the initial safe namespace topology." }

        Set-JournalPhase 'aborted'
        Remove-ControlTreeBestEffort $quarantine $quarantineRoot
        Remove-ControlTreeBestEffort $staging $stagingRoot
        Remove-Item -LiteralPath $journalPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $stateDir -Recurse -Force -ErrorAction SilentlyContinue
        throw $installError
    }
} finally {
    if ($null -ne $lockHandle) { $lockHandle.Dispose() }
}
