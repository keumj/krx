@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "EXIT_CODE=0"
set "REFRESH_OK=0"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

if "%FRED_API_KEY%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v FRED_API_KEY 2^>nul') do set "FRED_API_KEY=%%B"
)

echo.
echo ============================================================
echo  Keumj local data refresh
echo ============================================================
echo  This updates local data files only.
echo  After refresh, commit and push the changed Git LFS data files.
echo.
echo  Python: %PYTHON_EXE%
echo.
echo  [1] Refresh KRX prices / market caps / shared SQLite
echo  [2] Refresh KRX DART quarterly fundamentals in shared SQLite
echo  [3] Refresh KRX news in shared SQLite
echo  [4] Refresh KRX macro market/FRED data in macro SQLite
echo  [5] Run all refresh jobs
echo  [0] Exit
echo.

if not "%~1"=="" (
  set "CHOICE=%~1"
) else (
  set /p "CHOICE=Choose an option: "
)

if "%CHOICE%"=="0" goto :done
if "%CHOICE%"=="1" goto :stock
if "%CHOICE%"=="2" goto :quarterly
if "%CHOICE%"=="3" goto :news
if "%CHOICE%"=="4" goto :macro
if "%CHOICE%"=="5" goto :all
if /i "%CHOICE%"=="stock" goto :stock
if /i "%CHOICE%"=="quarterly" goto :quarterly
if /i "%CHOICE%"=="news" goto :news
if /i "%CHOICE%"=="macro" goto :macro
if /i "%CHOICE%"=="all" goto :all

echo Invalid option.
goto :done

:stock
call :run_module pipeline_common.refresh_krx_shared_prices
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
set "REFRESH_OK=1"
call :show_latest stock
goto :status

:quarterly
call :run_module pipeline_krx.refresh_dart_auto_fundamentals
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
set "REFRESH_OK=1"
call :show_latest quarterly
goto :status

:news
call :run_module pipeline_common.refresh_krx_news
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
set "REFRESH_OK=1"
call :show_latest news
goto :status

:macro
call :run_module pipeline_krx_macro.refresh_macro_prices
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
set "REFRESH_OK=1"
call :show_latest macro
goto :status

:all
call :run_module pipeline_common.refresh_krx_shared_prices
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest stock
call :run_module pipeline_krx.refresh_dart_auto_fundamentals
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest quarterly
call :run_module pipeline_common.refresh_krx_news
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest news
call :run_module pipeline_krx_macro.refresh_macro_prices
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest macro
set "REFRESH_OK=1"
goto :status

:run_module
echo.
echo ------------------------------------------------------------
echo Running: %~1
echo ------------------------------------------------------------
"%PYTHON_EXE%" -u -m %~1
exit /b %ERRORLEVEL%

:show_latest
echo.
echo ------------------------------------------------------------
echo Latest status: %~1
echo ------------------------------------------------------------
"%PYTHON_EXE%" scripts\refresh_status.py %~1
exit /b 0

:status
echo.
echo ============================================================
echo  Refresh finished. Changed files:
echo ============================================================
if "%REFRESH_OK%"=="1" (
  echo  Refresh result: SUCCESS
) else (
  echo  Refresh result: FAILED ^(exit_code=%EXIT_CODE%^)
)
echo.
git status --short data
echo.
echo Git LFS tracked data files:
git lfs ls-files
echo.
echo GitHub auto sync is disabled. Review changed SQLite files and push manually if needed.
echo.
echo Suggested next steps:
echo   git add data
echo   git commit -m "Refresh KRX market data"
echo   git push
echo.

:done
endlocal & exit /b %EXIT_CODE%
