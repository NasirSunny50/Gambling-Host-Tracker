@echo off
REM Make sure the project virtualenv exists with its dependencies installed.
REM Idempotent: does the full setup only the first time, then returns instantly.
cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" goto :ok

echo First-time setup: creating the virtual environment...
python -m venv .venv
if errorlevel 1 (
  echo.
  echo Could not create the virtualenv. Make sure Python is installed and on PATH.
  exit /b 1
)

echo Installing dependencies. This happens once and can take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[browser,api]"
if errorlevel 1 (
  echo.
  echo Dependency install failed. See the messages above.
  exit /b 1
)

echo Downloading the browser used for collection...
".venv\Scripts\python.exe" -m playwright install chromium

:ok
exit /b 0
