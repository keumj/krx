@echo off
setlocal EnableExtensions

cd /d "%~dp0"

if not exist "outputs" mkdir "outputs"

set "LOG_FILE=%CD%\outputs\refresh_local_data_scheduler.log"
set "KEUMJM_AUTO_SYNC_SHARED_DB=0"

if exist ".venv\Scripts\python.exe" (
  set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"
) else (
  set "PYTHON_EXE=python"
)

if "%FRED_API_KEY%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v FRED_API_KEY 2^>nul') do set "FRED_API_KEY=%%B"
)
if "%KOREA_ECOS_API_KEY%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v KOREA_ECOS_API_KEY 2^>nul') do set "KOREA_ECOS_API_KEY=%%B"
)

echo.>>"%LOG_FILE%"
echo ============================================================>>"%LOG_FILE%"
echo [%date% %time%] Scheduled KRX refresh started>>"%LOG_FILE%"
echo ============================================================>>"%LOG_FILE%"

call ensure_lan_server.cmd >>"%LOG_FILE%" 2>&1

"%PYTHON_EXE%" scripts\record_refresh_state.py started --source scheduled >>"%LOG_FILE%" 2>&1

call refresh_local_data.cmd 5 >>"%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

echo.>>"%LOG_FILE%"
echo ------------------------------------------------------------>>"%LOG_FILE%"
echo [%date% %time%] KOSPI200 daily constituent history sync>>"%LOG_FILE%"
echo ------------------------------------------------------------>>"%LOG_FILE%"
"%PYTHON_EXE%" -m pipeline_krx.benchmark --mode history --source-mode pykrx_index --db-path data\krx_shared_db\krx_shared_prices.sqlite >>"%LOG_FILE%" 2>&1
set "BENCHMARK_EXIT_CODE=%ERRORLEVEL%"
echo [%date% %time%] KOSPI200 history sync finished with exit_code=%BENCHMARK_EXIT_CODE%>>"%LOG_FILE%"

"%PYTHON_EXE%" scripts\record_refresh_state.py finished --source scheduled --exit-code %EXIT_CODE% >>"%LOG_FILE%" 2>&1

if "%EXIT_CODE%"=="0" (
  echo.>>"%LOG_FILE%"
  echo ------------------------------------------------------------>>"%LOG_FILE%"
  echo KRX SQLite auto sync is disabled. Review the start page and push manually if needed.>>"%LOG_FILE%"
  echo ------------------------------------------------------------>>"%LOG_FILE%"
)

echo [%date% %time%] Scheduled KRX refresh finished with exit_code=%EXIT_CODE%>>"%LOG_FILE%"
echo.>>"%LOG_FILE%"

endlocal & exit /b %EXIT_CODE%
