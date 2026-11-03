$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSCommandPath
$python = Join-Path $root '.work\venv314\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Workflowfähiger Python-3.14-Interpreter fehlt: $python. Lege die isolierte .work\venv314-Umgebung an und installiere dort .[dev,workflows]; die normale .venv bleibt unverändert."
}

$version = & $python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $version.Trim() -ne '3.14') {
    throw "Der Workflow-Launcher benötigt Python 3.14 in .work\venv314; gefunden: $($version.Trim())."
}

$previousPythonPath = $env:PYTHONPATH
$previousLocation = (Get-Location).Path
try {
    Set-Location -LiteralPath $root
    $env:PYTHONPATH = Join-Path $root 'src'
    & $python -m dbzscc
    $appExitCode = $LASTEXITCODE
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    Set-Location -LiteralPath $previousLocation
}

exit $appExitCode
