@echo off
setlocal
set PYTHONUTF8=1

set "PYTHON_EXE="
set "PYTHON_ARGS="

where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set "PYTHON_EXE=py"
  set "PYTHON_ARGS=-3"
  goto :found_python
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
  python --version >nul 2>nul
  if %ERRORLEVEL%==0 (
    set "PYTHON_EXE=python"
    set "PYTHON_ARGS="
    goto :found_python
  )
)

if exist "C:\Program Files\Blender Foundation\Blender 3.4\3.4\python\bin\python.exe" (
  set "PYTHON_EXE=C:\Program Files\Blender Foundation\Blender 3.4\3.4\python\bin\python.exe"
  set "PYTHON_ARGS="
  goto :found_python
)

echo Python was not found.
echo Install Python 3.10 or 3.11, then run setup_service.cmd again.
exit /b 1

:found_python
echo Using Python: "%PYTHON_EXE%" %PYTHON_ARGS%

if not exist ".venv\Scripts\python.exe" (
  "%PYTHON_EXE%" %PYTHON_ARGS% -m venv .venv
  if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%
)

".venv\Scripts\python.exe" -m pip install --upgrade pip
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if %ERRORLEVEL% neq 0 exit /b %ERRORLEVEL%

echo.
echo Setup complete. Run:
echo   run_service.cmd
exit /b 0
