@echo off
REM Double-click this to use the tracker.
REM Opens the portal in your browser. Keep this window open while you work;
REM close it (or press Ctrl+C) to stop.
title Host Tracker
cd /d "%~dp0.."

call "scripts\internal\_ensure-env.bat"
if errorlevel 1 ( echo. & pause & exit /b 1 )

REM Stop any portal or collection browser still running from a previous launch, so this
REM one binds the port cleanly and runs the current code rather than an old copy held in
REM memory. Safe when nothing is running.
call "scripts\internal\_stop-portal.bat"

echo.
echo ============================================================
echo   Host Tracker
echo ============================================================
echo Starting the portal and opening it in your browser.
echo The exact address is printed just below (usually
echo http://127.0.0.1:8000; a busy port shifts it to the next one).
echo.
echo On the Runs page, press "Run collection". If the site needs a
echo sign-in, a browser window opens for you - sign in there and
echo collection continues by itself.
echo.
echo Keep this window open. Close it (or press Ctrl+C) to stop.
echo.

REM serve.py picks a free port if 8000 is taken and opens the browser on the real address
REM once the server is answering, so the two can never disagree about the port.
".venv\Scripts\python.exe" scripts\internal\serve.py --open

echo.
echo Portal stopped.
pause
