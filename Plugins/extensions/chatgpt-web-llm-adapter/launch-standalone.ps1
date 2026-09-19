[CmdletBinding()]
param(
    [ValidateSet('start','probe','stop','status')]
    [string]$Mode = 'start'
)

$ErrorActionPreference = 'Stop'
$pluginRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$repoRoot = [IO.Path]::GetFullPath((Join-Path $pluginRoot '..\..\..'))
$script = Join-Path $pluginRoot 'standalone.py'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
    throw "standalone.py is missing: $script"
}

$python = $null
$prefix = @()
$venvPython = Join-Path $repoRoot '.build-venv\Scripts\python.exe'
if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $python = $venvPython
}
if (-not $python) {
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        $python = $py.Source
        $prefix = @('-3')
    }
}
if (-not $python) {
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($systemPython) {
        $python = $systemPython.Source
    }
}
if (-not $python) {
    throw 'Python 3 was not found. Install Python 3 or build the repository .build-venv first.'
}

$arguments = @($prefix) + @($script)
switch ($Mode) {
    'probe'  { $arguments += '--probe' }
    'stop'   { $arguments += '--stop' }
    'status' { $arguments += '--status' }
}

Write-Host "ChatGPT Web LLM Adapter Standalone 0.3.2"
Write-Host "Mode: $Mode"
Write-Host "Python: $python"
& $python @arguments
$code = $LASTEXITCODE
if ($Mode -ne 'start') {
    Write-Host ''
    if ($code -eq 0) { Write-Host 'Completed.' -ForegroundColor Green }
    else { Write-Host "Exit code: $code" -ForegroundColor Yellow }
}
exit $code
