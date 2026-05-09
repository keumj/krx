@echo off
net session >nul 2>nul
if %ERRORLEVEL% neq 0 (
  echo Please run this file as Administrator.
  exit /b 1
)

if "%KEUMJM_PORT%"=="" set KEUMJM_PORT=8515

netsh advfirewall firewall add rule name="Keumj KRX Lab LAN %KEUMJM_PORT%" dir=in action=allow protocol=TCP localport=%KEUMJM_PORT% profile=private
echo Firewall rule added for TCP port %KEUMJM_PORT% on the Private network profile.
