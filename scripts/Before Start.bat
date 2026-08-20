@echo off
REM Run this once (and again whenever collection says the session expired).
REM Opens a real browser so you can sign in to 1xBet yourself; only the resulting
REM session is saved, never your password.
title Host Tracker - Login
cd /d "%~dp0.."

call "scripts\_ensure-env.bat"
if errorlevel 1 ( echo. & pause & exit /b 1 )

echo.
echo ============================================================
echo   Host Tracker  -  Login
echo ============================================================
echo A browser window will open. Sign in to 1xBet yourself, then
echo come back here and press Enter. Your password is never read
echo or stored - only the resulting session is saved.
echo.
".venv\Scripts\python.exe" scripts\collect_1xbet.py --login

echo.
echo Done. You can close this window and run Start.
pause
