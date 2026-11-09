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

echo Starting: desktop heart-rate window
%PY% hr_window.py
echo.
echo Exited (code %ERRORLEVEL%). Press any key to close.
pause >nul
