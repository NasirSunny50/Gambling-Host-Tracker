@echo off
REM ============================================================================
REM Gambling Host Tracker - one-time setup for a fresh PC, with nothing to click.
REM
REM Copy the project folder to the new machine and double-click this file. It
REM installs everything the tracker needs - Python itself if the PC does not have
REM it, then the virtualenv, the dependencies, and the browser - seeds the
REM settings file, and leaves it ready to run. Safe to run again: anything already
REM done is skipped, so an interrupted setup just resumes.
REM
REM To set up AND launch in one go, use Start.bat instead - it does all of this
REM and then opens the portal.
REM ============================================================================
title Gambling Host Tracker - Setup
cd /d "%~dp0.."

echo.
echo ============================================================
echo   Gambling Host Tracker - setup
echo ============================================================
echo.

REM Python (installed if missing), the virtualenv, all dependencies, the browser.
call "scripts\internal\_ensure-env.bat"
if errorlevel 1 (
  echo.
  echo [X] Setup could not finish. See the messages above.
  pause
  exit /b 1
)
echo [ok] Python, dependencies and browser are ready.

REM Seed the settings file the first time. .env holds settings and the sign-in
REM credentials and is gitignored, so a fresh copy does not have one - start it
REM from the committed example, credentials left blank.
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

REM The database, evidence and saved sessions live under data\. The code creates
REM these on demand; making them now means the first run has nothing to set up.
if not exist "data\evidence" mkdir "data\evidence" 2>nul
if not exist "data\auth" mkdir "data\auth" 2>nul
if not exist "data\branding" mkdir "data\branding" 2>nul
echo [ok] Data folders ready.

REM A quick check that everything the portal needs imports.
".venv\Scripts\python.exe" -c "import ght.api.routes, ght.fetchers.browser, ght.export.report" 2>nul
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
