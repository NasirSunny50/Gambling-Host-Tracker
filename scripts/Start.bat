@echo off
REM Starts the portal (and its backend). Keep this window open while you use it;
REM close it or press Ctrl+C to stop. Trigger collections from the Runs page.
title Host Tracker - Portal
cd /d "%~dp0.."

call "scripts\_ensure-env.bat"
if errorlevel 1 ( echo. & pause & exit /b 1 )

echo.
echo ============================================================
echo   Host Tracker  -  Portal
echo ============================================================
echo Opening http://127.0.0.1:8000 in your browser.
echo Keep this window open. Close it (or press Ctrl+C) to stop.
echo.

REM Open the browser a few seconds later, once the server is up.
start "" cmd /c "timeout /t 3 /nobreak >nul & start "" http://127.0.0.1:8000"

".venv\Scripts\python.exe" scripts\serve.py

echo.
echo Portal stopped.
pause
