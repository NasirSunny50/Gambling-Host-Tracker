@echo off
REM Double-click this when the portal will not start.
REM It checks everything at once - the code on disk, the port, whatever holds the port,
REM Windows' reserved ranges, and leftover processes - and ends with a verdict.
REM It only reads. It kills nothing and changes nothing.
title Host Tracker - check

cd /d "%~dp0.."

if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\internal\doctor.py
) else (
  REM The virtualenv may be the very thing that is missing, so fall back to any Python:
  REM the check imports nothing from the project and runs on a bare interpreter.
  python scripts\internal\doctor.py
)

echo.
pause
