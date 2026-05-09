@echo off
setlocal
set PYTHONUTF8=1

if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="KEUMJM_ACCESS_MODE" if "%KEUMJM_ACCESS_MODE%"=="" set "KEUMJM_ACCESS_MODE=%%B"
    if /i "%%A"=="KEUMJM_HOST" if "%KEUMJM_HOST%"=="" set "KEUMJM_HOST=%%B"
    if /i "%%A"=="KEUMJM_PORT" if "%KEUMJM_PORT%"=="" set "KEUMJM_PORT=%%B"
    if /i "%%A"=="ENABLE_MACRO" if "%ENABLE_MACRO%"=="" set "ENABLE_MACRO=%%B"
  )
)

if "%KEUMJM_ACCESS_MODE%"=="" set KEUMJM_ACCESS_MODE=lan
if "%KEUMJM_HOST%"=="" set KEUMJM_HOST=0.0.0.0
if "%KEUMJM_PORT%"=="" set KEUMJM_PORT=8516
if "%ENABLE_MACRO%"=="" set ENABLE_MACRO=1
if "%KEUMJM_AUTH_COOKIE_SECURE%"=="" set KEUMJM_AUTH_COOKIE_SECURE=1
if "%KEUMJM_SSL_CERTFILE%"=="" set KEUMJM_SSL_CERTFILE=certs\keumjm-lan.crt
if "%KEUMJM_SSL_KEYFILE%"=="" set KEUMJM_SSL_KEYFILE=certs\keumjm-lan.key

set "PYTHON_EXE=python"
if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

if not exist "%KEUMJM_SSL_CERTFILE%" (
  "%PYTHON_EXE%" scripts\create_https_cert.py --certfile "%KEUMJM_SSL_CERTFILE%" --keyfile "%KEUMJM_SSL_KEYFILE%"
  if errorlevel 1 exit /b %ERRORLEVEL%
)
if not exist "%KEUMJM_SSL_KEYFILE%" (
  "%PYTHON_EXE%" scripts\create_https_cert.py --certfile "%KEUMJM_SSL_CERTFILE%" --keyfile "%KEUMJM_SSL_KEYFILE%"
  if errorlevel 1 exit /b %ERRORLEVEL%
)

echo.
echo Keumj KRX Lab HTTPS LAN mode
echo Local:   https://localhost:%KEUMJM_PORT%
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } | ForEach-Object { 'LAN:     https://' + $_.IPAddress + ':%KEUMJM_PORT%' }"
echo Mode:    %KEUMJM_ACCESS_MODE%
echo Macro:   %ENABLE_MACRO%
echo.

"%PYTHON_EXE%" scripts\run_uvicorn.py
exit /b %ERRORLEVEL%
