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
if "%KOREA_ECOS_API_KEY%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v KOREA_ECOS_API_KEY 2^>nul') do set "KOREA_ECOS_API_KEY=%%B"
)
if "%NAVER_NEWS_CLIENT_ID%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v NAVER_NEWS_CLIENT_ID 2^>nul') do set "NAVER_NEWS_CLIENT_ID=%%B"
)
if "%NAVER_NEWS_CLIENT_SECRET%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v NAVER_NEWS_CLIENT_SECRET 2^>nul') do set "NAVER_NEWS_CLIENT_SECRET=%%B"
)
if "%NAVER_NEWS_CLIENT_ID%"=="" if not "%NAVER_CLIENT_ID%"=="" set "NAVER_NEWS_CLIENT_ID=%NAVER_CLIENT_ID%"
if "%NAVER_NEWS_CLIENT_SECRET%"=="" if not "%NAVER_CLIENT_SECRET%"=="" set "NAVER_NEWS_CLIENT_SECRET=%NAVER_CLIENT_SECRET%"
if "%NAVER_NEWS_CLIENT_ID%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v NAVER_CLIENT_ID 2^>nul') do set "NAVER_NEWS_CLIENT_ID=%%B"
)
if "%NAVER_NEWS_CLIENT_SECRET%"=="" (
  for /f "tokens=2,*" %%A in ('reg query HKCU\Environment /v NAVER_CLIENT_SECRET 2^>nul') do set "NAVER_NEWS_CLIENT_SECRET=%%B"
)

echo.
echo ============================================================
echo  Keumj local data refresh
echo ============================================================
echo  This updates local data files only.
echo.
echo  Python: %PYTHON_EXE%
echo.
echo  [1] Refresh KRX prices / market caps / shares / shared SQLite ^(+ EPS backfill^)
echo  [2] Refresh KRX DART quarterly fundamentals in shared SQLite ^(+ shares/EPS sync^)
echo  [3] Refresh KRX news incrementally ^(Naver + Google low-coverage supplement^)
echo  [4] Refresh KRX macro market/FRED data in macro SQLite
echo  [5] Run latest-data refresh jobs ^(without news^)
echo  [6] Run latest-data refresh jobs ^(with news^)
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
if "%CHOICE%"=="6" goto :all_with_news
if /i "%CHOICE%"=="stock" goto :stock
if /i "%CHOICE%"=="quarterly" goto :quarterly
if /i "%CHOICE%"=="news" goto :news
if /i "%CHOICE%"=="macro" goto :macro
if /i "%CHOICE%"=="all" goto :all
if /i "%CHOICE%"=="all-news" goto :all_with_news
if /i "%CHOICE%"=="with-news" goto :all_with_news

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
call :ensure_naver_news_credentials
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :run_module pipeline_krx.refresh_news --provider naver --max-items 100 --timeout 10 --request-delay 0.1 --components-csv data\krx_components.csv --db-path data\krx_shared_db\krx_shared_prices.sqlite
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :run_module pipeline_krx.refresh_news --provider google --max-items 100 --timeout 10 --request-delay 0.1 --google-hl ko --google-gl KR --google-ceid KR:ko --max-existing-articles 3 --components-csv data\krx_components.csv --db-path data\krx_shared_db\krx_shared_prices.sqlite
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
set "REFRESH_OK=1"
call :show_latest news
goto :status

:macro
call :run_module pipeline_krx_macro.refresh_macro_prices --years 10 --daily-core --require-ecos
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
call :run_module pipeline_krx_macro.refresh_macro_prices --years 10 --daily-core --require-ecos
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest macro
set "REFRESH_OK=1"
goto :status

:all_with_news
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
call :run_module pipeline_krx_macro.refresh_macro_prices --years 10 --daily-core --require-ecos
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest macro
call :ensure_naver_news_credentials
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :run_module pipeline_krx.refresh_news --provider naver --max-items 100 --timeout 10 --request-delay 0.1 --components-csv data\krx_components.csv --db-path data\krx_shared_db\krx_shared_prices.sqlite
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :run_module pipeline_krx.refresh_news --provider google --max-items 100 --timeout 10 --request-delay 0.1 --google-hl ko --google-gl KR --google-ceid KR:ko --max-existing-articles 3 --components-csv data\krx_components.csv --db-path data\krx_shared_db\krx_shared_prices.sqlite
if errorlevel 1 (
  set "EXIT_CODE=%ERRORLEVEL%"
  goto :status
)
call :show_latest news
set "REFRESH_OK=1"
goto :status

:ensure_naver_news_credentials
if "%NAVER_NEWS_CLIENT_ID%"=="" (
  echo.
  echo ------------------------------------------------------------
  echo Missing NAVER_NEWS_CLIENT_ID.
  echo Set NAVER_NEWS_CLIENT_ID and NAVER_NEWS_CLIENT_SECRET before running news refresh.
  echo ------------------------------------------------------------
  exit /b 1
)
if "%NAVER_NEWS_CLIENT_SECRET%"=="" (
  echo.
  echo ------------------------------------------------------------
  echo Missing NAVER_NEWS_CLIENT_SECRET.
  echo Set NAVER_NEWS_CLIENT_ID and NAVER_NEWS_CLIENT_SECRET before running news refresh.
  echo ------------------------------------------------------------
  exit /b 1
)
exit /b 0

:run_module
echo.
echo ------------------------------------------------------------
echo Running: %*
echo ------------------------------------------------------------
"%PYTHON_EXE%" -u -m %*
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
echo  Refresh finished.
echo ============================================================
if "%REFRESH_OK%"=="1" (
  echo  Refresh result: SUCCESS
) else (
  echo  Refresh result: FAILED ^(exit_code=%EXIT_CODE%^)
)
echo.

:done
endlocal & exit /b %EXIT_CODE%
