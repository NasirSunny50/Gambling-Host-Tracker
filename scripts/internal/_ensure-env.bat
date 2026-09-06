@echo off
REM Make sure the project virtualenv exists AND actually works - and, before that,
REM that there is a Python to build it with at all. Installs whatever is missing,
REM from Python itself down to the browser, with nothing to click.
REM Idempotent: does the full setup only the first time, then returns instantly.
cd /d "%~dp0..\.."

REM A venv is only "there" if its python actually runs. One created against a Python
REM that was then moved - or a half-written one - leaves the launcher on disk but
REM unable to find its base, which surfaces much later as "No Python at ...". So the
REM check runs it, not just tests that the file exists, and rebuilds a broken one.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
  if not errorlevel 1 goto :ok
  echo [!] The existing virtual environment is broken - rebuilding it...
  rmdir /s /q ".venv"
)

REM No usable venv, so this is a first run (or a repair). Make sure Python exists
REM first, installing it if it does not; _ensure-python leaves it to use in GHT_PY.
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

REM Prove the fresh venv runs before leaning on it, so a bad build fails here with a
REM clear message instead of hours later when the portal cannot start.
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo.
  echo [X] The virtual environment was created but its Python will not run.
  echo     Removing it so the next launch rebuilds cleanly.
  rmdir /s /q ".venv" 2>nul
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
