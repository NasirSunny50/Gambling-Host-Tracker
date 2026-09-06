@echo off
REM Make sure the project virtualenv exists with its dependencies installed - and,
REM before that, that there is a Python to build it with at all. Installs whatever
REM is missing, from Python itself down to the browser, with nothing to click.
REM Idempotent: does the full setup only the first time, then returns instantly.
cd /d "%~dp0..\.."

if exist ".venv\Scripts\python.exe" goto :ok

REM No venv yet, so this is a first run. Make sure Python exists first, installing
REM it if it does not; _ensure-python leaves the interpreter to use in GHT_PY.
call "%~dp0_ensure-python.bat"
if errorlevel 1 exit /b 1
if not defined GHT_PY set "GHT_PY=python"

echo [..] Creating the virtual environment...
%GHT_PY% -m venv .venv
if errorlevel 1 (
  echo.
  echo Could not create the virtualenv. See the messages above.
  exit /b 1
)

echo [..] Installing dependencies. This happens once and can take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[browser,api,export]"
if errorlevel 1 (
  echo.
  echo Dependency install failed. See the messages above.
  exit /b 1
)

echo [..] Downloading the browser used for collection...
".venv\Scripts\python.exe" -m playwright install chromium

:ok
exit /b 0
