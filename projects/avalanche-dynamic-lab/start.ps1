param([int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# The solver needs AvaFrame's Python, not whatever `python` happens to be first on
# PATH. Resolution order: AVAFRAME_PYTHON, the path used on the development
# machine, then any python on PATH that can actually import avaframe.
function Test-AvaFramePython([string]$Exe) {
    if (-not $Exe -or -not (Test-Path -LiteralPath $Exe)) { return $false }
    try {
        & $Exe -c 'import avaframe' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$python = $null
foreach ($candidate in @($env:AVAFRAME_PYTHON, 'D:\AvaFrame_demo\.pixi\envs\default\python.exe')) {
    if (Test-AvaFramePython $candidate) { $python = $candidate; break }
}
if (-not $python) {
    foreach ($command in (Get-Command python, python3 -ErrorAction SilentlyContinue)) {
        if (Test-AvaFramePython $command.Source) { $python = $command.Source; break }
    }
}
if (-not $python) {
    throw 'No Python with AvaFrame was found. Set the AVAFRAME_PYTHON environment variable to the AvaFrame interpreter, or install AvaFrame. See README.md.'
}
Write-Host "Using AvaFrame Python: $python"

if (-not (Test-Path -LiteralPath 'node_modules\cesium\Build\Cesium\Cesium.js')) {
    & npm.cmd install --ignore-scripts --no-audit --no-fund --cache .cache/npm
    if ($LASTEXITCODE -ne 0) { throw 'Cesium dependency installation failed.' }
}
$env:PYTHONIOENCODING = 'utf-8'
$address = "http://127.0.0.1:$Port"
try {
    $existing = Invoke-RestMethod -Uri "$address/api/status" -TimeoutSec 2
    if ($existing.project -eq $PSScriptRoot) {
        Write-Host "Snowwork 3D is already running: $address"
        if (-not $NoBrowser) { Start-Process $address }
        return
    }
} catch { }
if ($NoBrowser) { & $python -B server.py --port $Port --no-browser }
else { & $python -B server.py --port $Port }
