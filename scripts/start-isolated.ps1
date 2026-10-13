param([int]$Port = 8511)
$ErrorActionPreference = 'Stop'
$isolatedRoot = Split-Path -Parent $PSScriptRoot
$isolatedPython = Join-Path $isolatedRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $isolatedPython)) {
    $isolatedPython = Join-Path (Split-Path -Parent $isolatedRoot) 'AI-Risk-Based-Inventory-ERP-new\.venv\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $isolatedPython)) {
    throw 'Python environment missing. Create .venv and install requirements.txt plus requirements-dev.txt.'
}
& $isolatedPython (Join-Path $PSScriptRoot 'run_isolated.py') --port $Port
