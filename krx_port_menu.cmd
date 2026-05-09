@echo off
setlocal EnableExtensions

cd /d "%~dp0"

if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="KEUMJM_HOST" if "%KEUMJM_HOST%"=="" set "KEUMJM_HOST=%%B"
    if /i "%%A"=="KEUMJM_PORT" if "%KEUMJM_PORT%"=="" set "KEUMJM_PORT=%%B"
    if /i "%%A"=="KEUMJM_ACCESS_MODE" if "%KEUMJM_ACCESS_MODE%"=="" set "KEUMJM_ACCESS_MODE=%%B"
    if /i "%%A"=="ENABLE_MACRO" if "%ENABLE_MACRO%"=="" set "ENABLE_MACRO=%%B"
  )
)

if "%KEUMJM_HOST%"=="" set "KEUMJM_HOST=0.0.0.0"
if "%KEUMJM_PORT%"=="" set "KEUMJM_PORT=8516"
if "%KEUMJM_ACCESS_MODE%"=="" set "KEUMJM_ACCESS_MODE=lan"
if "%ENABLE_MACRO%"=="" set "ENABLE_MACRO=1"

:menu
cls
echo.
echo ==============================
echo   Keumj KRX Port Menu
echo ==============================
echo.
echo Target port: %KEUMJM_PORT%
echo.
echo 1. Open port  - start KRX LAN service
echo 2. Close port - force stop process on target port
echo Q. Quit
echo.
choice /c 12Q /n /m "Select [1/2/Q]: "

if errorlevel 3 goto end
if errorlevel 2 goto close_port
if errorlevel 1 goto open_port

:open_port
echo.
echo Starting KRX LAN service...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = [int]$env:KEUMJM_PORT; " ^
  "$root = (Get-Location).Path; " ^
  "$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; " ^
  "if ($listener) { Write-Host ('Already listening on port ' + $port + '. PID=' + $listener.OwningProcess); exit 0 }; " ^
  "$script = Join-Path $root 'run_lan_service.cmd'; " ^
  "if (-not (Test-Path $script)) { Write-Error ('Missing script: ' + $script); exit 1 }; " ^
  "Start-Process -FilePath $script -WorkingDirectory $root; " ^
  "Start-Sleep -Seconds 2; " ^
  "$started = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; " ^
  "if ($started) { Write-Host ('Port ' + $port + ' is open. PID=' + $started.OwningProcess); exit 0 }; " ^
  "Write-Host ('Start requested. If port ' + $port + ' is not listening yet, check the new server window.'); exit 0"
echo.
pause
goto menu

:close_port
echo.
if "%KEUMJM_PORT%"=="8515" (
  echo Port 8515 is protected for the SP500 scheduled service. Not stopping it.
  echo Check KEUMJM_PORT in .env.
  echo.
  pause
  goto menu
)

echo Stopping process listening on port %KEUMJM_PORT%...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$port = [int]$env:KEUMJM_PORT; " ^
  "if ($port -eq 8515) { Write-Host 'Port 8515 is protected. Not stopping it.'; exit 2 }; " ^
  "$listeners = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; " ^
  "if (-not $listeners) { Write-Host ('No listener found on port ' + $port + '.'); exit 0 }; " ^
  "$pids = $listeners | Select-Object -ExpandProperty OwningProcess -Unique; " ^
  "foreach ($pidValue in $pids) { " ^
  "  try { Stop-Process -Id $pidValue -Force -ErrorAction Stop; Write-Host ('Stopped PID=' + $pidValue) } " ^
  "  catch { Write-Host ('Failed to stop PID=' + $pidValue + ' / ' + $_.Exception.Message) } " ^
  "}"
echo.
pause
goto menu

:end
endlocal
exit /b 0
