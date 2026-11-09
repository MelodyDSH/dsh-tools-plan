@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [x] Python not found. Install Python 3.9+ and retry.
  pause
  exit /b 1
)

set "PYTHONIOENCODING=utf-8"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

%PY% -c "import bleak" >nul 2>nul
if errorlevel 1 (
  echo [.] First run: installing dependencies, please wait...
  %PY% -m pip install --disable-pip-version-check -q -r requirements.txt
  if errorlevel 1 (
    echo.
    echo [x] Failed to install dependencies.
    echo     Check your network, then run this in this folder:
    echo         %PY% -m pip install -r requirements.txt
    pause
    exit /b 1
  )
  echo [.] Dependencies installed.
)

echo Starting: heart-rate service on http://127.0.0.1:8787
%PY% hr_service.py
echo.
echo Exited (code %ERRORLEVEL%). Press any key to close.
pause >nul
