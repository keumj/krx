$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
if (-not $env:ENABLE_MACRO) { $env:ENABLE_MACRO = "1" }

$python = "python"
if (Test-Path ".venv\Scripts\python.exe") {
  $python = ".venv\Scripts\python.exe"
}

& $python scripts/run_uvicorn.py
