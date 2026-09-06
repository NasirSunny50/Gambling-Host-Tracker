@echo off
REM ============================================================================
REM Gambling Host Tracker - one-time setup for a fresh PC.
REM
REM Copy the project folder to the new machine, then double-click this file. It
REM installs everything the tracker needs and leaves it ready to run. Safe to run
REM again: anything already done is skipped, so a half-finished setup just resumes.
REM
REM The only thing it cannot install for you is Python itself - Windows has no
REM built-in package manager to lean on - so if Python is missing it stops and
REM tells you exactly where to get it. Everything after that is automatic.
REM ============================================================================
title Gambling Host Tracker - Setup
cd /d "%~dp0.."

echo.
echo ============================================================
echo   Gambling Host Tracker - setup
echo ============================================================
echo.

REM --- 1. Find a Python interpreter -------------------------------------------
REM Prefer the "py" launcher, which is version-aware and skips the Microsoft Store
REM stub that "python" often resolves to on a fresh Windows. Fall back to python.
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [X] Python is not installed, or not on PATH.
  echo.
  echo     Install Python 3.11 or newer from:
  echo         https://www.python.org/downloads/
  echo.
  echo     IMPORTANT: on the first screen of the installer, tick
  echo     "Add python.exe to PATH" before clicking Install.
  echo.
  echo     Then run this Setup again.
  echo.
  pause
  exit /b 1
)

REM --- 2. Check the version is new enough -------------------------------------
%PY% -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)"
if errorlevel 1 (
  echo [X] Found Python, but it is older than 3.11.
  %PY% --version
  echo.
  echo     Install Python 3.11 or newer from https://www.python.org/downloads/
  echo     and run this Setup again.
  echo.
  pause
  exit /b 1
)
for /f "delims=" %%v in ('%PY% --version 2^>^&1') do echo [ok] %%v found

REM --- 3. Create the virtual environment --------------------------------------
if exist ".venv\Scripts\python.exe" (
  echo [ok] Virtual environment already exists.
) else (
  echo [..] Creating the virtual environment...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo.
    echo [X] Could not create the virtual environment. See the messages above.
    pause
    exit /b 1
  )
  echo [ok] Virtual environment created.
)
set "VENV=.venv\Scripts\python.exe"

REM --- 4. Install the project and its dependencies ----------------------------
REM All three extras: browser (Playwright collection), api (the portal),
REM export (the PDF and Excel reports). This is the full install, so nothing on
REM the machine is missing a feature the portal offers.
echo [..] Installing dependencies. First time this can take a few minutes...
"%VENV%" -m pip install --upgrade pip
"%VENV%" -m pip install -e ".[browser,api,export]"
if errorlevel 1 (
  echo.
  echo [X] Dependency install failed. See the messages above.
  echo     A common cause is no internet connection.
  pause
  exit /b 1
)
echo [ok] Dependencies installed.

REM --- 5. Download the browser Playwright drives ------------------------------
echo [..] Downloading the Chromium browser used for collection...
"%VENV%" -m playwright install chromium
if errorlevel 1 (
  echo.
  echo [X] The browser download failed. See the messages above.
  echo     Collection needs it; the portal will still open without it.
  pause
  exit /b 1
)
echo [ok] Browser ready.

REM --- 6. Seed the settings file the first time -------------------------------
REM .env holds settings and the sign-in credentials, and is gitignored so it
REM never travels with the code. On a fresh copy it is missing, so seed it from
REM the committed example - filled with safe defaults, credentials left blank.
if exist ".env" (
  echo [ok] Settings file .env already present - left untouched.
) else (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo [ok] Created .env from the example. Add your site sign-in details to it.
  ) else (
    echo [!] No .env.example to copy - you will need to create .env by hand.
  )
)

REM --- 7. Make sure the data folders exist ------------------------------------
REM The database, evidence and saved sessions all live under data\. The code
REM creates these on demand, but making them now means the first run has nothing
REM to set up.
if not exist "data\evidence" mkdir "data\evidence" 2>nul
if not exist "data\auth" mkdir "data\auth" 2>nul
if not exist "data\branding" mkdir "data\branding" 2>nul
echo [ok] Data folders ready.

REM --- 8. A quick check that it all imports -----------------------------------
"%VENV%" -c "import ght.api.routes, ght.fetchers.browser, ght.export.report" 2>nul
if errorlevel 1 (
  echo [!] Setup finished but a quick import check failed. Run scripts\Check.bat.
) else (
  echo [ok] Everything imports cleanly.
)

echo.
echo ============================================================
echo   Setup complete.
echo ============================================================
echo.
echo   Next: double-click scripts\Start.bat to open the portal.
echo.
echo   Before collecting, open .env and fill in the sign-in details
echo   for each site (the GHT_LOGIN_... lines). Without them a run
echo   opens a browser window for you to sign in by hand.
echo.
pause
exit /b 0
