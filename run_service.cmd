@echo off
setlocal
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8515
  exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3 -m uvicorn app.main:app --host 0.0.0.0 --port 8515
  exit /b %ERRORLEVEL%
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python -m uvicorn app.main:app --host 0.0.0.0 --port 8515
  exit /b %ERRORLEVEL%
)

if exist "C:\Program Files\Blender Foundation\Blender 3.4\3.4\python\bin\python.exe" (
  "C:\Program Files\Blender Foundation\Blender 3.4\3.4\python\bin\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8515
  exit /b %ERRORLEVEL%
)

echo Python was not found. Install Python, activate a virtual environment, or run setup_service.cmd first.
exit /b 1
