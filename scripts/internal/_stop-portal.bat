@echo off
REM Stop anything left running from an earlier launch, so a fresh Start always binds the
REM port and runs the current code. Two things get in the way:
REM   1. A portal (serve.py / uvicorn) still holding http://127.0.0.1:8000 - a second one
REM      cannot bind the port, and an old one keeps running stale code from memory.
REM   2. Headless browsers left behind by a collection that was killed mid-flight - they
REM      hold sockets and make the next fetch look like a network failure.
REM Idempotent and quiet: if nothing is running, it does nothing and returns 0.

where powershell >nul 2>&1
if errorlevel 1 goto :done

REM Kill any portal or collection Python belonging to this project. Matched on the command
REM line (serve.py, the ght console entry points, or `-m ght`) so unrelated Python on the
REM machine is left alone. Only single quotes inside the PowerShell so cmd needs no escaping.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'serve\.py|ght\.cli|ght\.api|-m ght' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Clear browsers a killed collection left behind (matched by their Playwright temp path).
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'Temp\\playwright' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

REM Give the OS a moment to release the listening socket before the new portal binds it.
timeout /t 1 /nobreak >nul

:done
exit /b 0
