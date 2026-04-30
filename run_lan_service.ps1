$ErrorActionPreference = "Stop"

if (-not $env:PYTHONUTF8) { $env:PYTHONUTF8 = "1" }
if (-not $env:KEUMJM_ACCESS_MODE) { $env:KEUMJM_ACCESS_MODE = "lan" }
if (-not $env:KEUMJM_HOST) { $env:KEUMJM_HOST = "0.0.0.0" }
if (-not $env:KEUMJM_PORT) { $env:KEUMJM_PORT = "8515" }

$python = "python"
if (Test-Path ".venv\Scripts\python.exe") {
  $python = ".venv\Scripts\python.exe"
}

$addresses = Get-NetIPAddress -AddressFamily IPv4 |
  Where-Object { $_.IPAddress -notlike "169.254.*" -and $_.IPAddress -ne "127.0.0.1" } |
  Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "Keumjm Portfolio Lab LAN mode"
Write-Host "Local:   http://localhost:$env:KEUMJM_PORT"
foreach ($address in $addresses) {
  Write-Host "LAN:     http://${address}:$env:KEUMJM_PORT"
}
Write-Host "Mode:    $env:KEUMJM_ACCESS_MODE"
Write-Host ""

& $python -m uvicorn app.main:app --host $env:KEUMJM_HOST --port $env:KEUMJM_PORT
