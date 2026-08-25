@echo off
REM Stop anything left running from an earlier launch, so this start binds the port cleanly
REM and runs the current code rather than an old copy held in memory. Safe when nothing is
REM running - it simply finds nothing to kill.

REM 1) The one that actually frees the port: kill whatever is LISTENING on 8000, whoever it
REM    is and however it was launched. Pure netstat/taskkill by PID - works on any Windows,
REM    needs no PowerShell, and does not depend on reading another process's command line
REM    (which can come back blank and was why a command-line match alone missed the portal).
for /f "tokens=5" %%p in ('netstat -ano ^| findstr "LISTENING" ^| findstr ":8000"') do (
  taskkill /F /PID %%p >nul 2>&1
)

REM 2) Belt and suspenders (this part needs PowerShell for the command line): a project
REM    portal that is still starting up and not yet listening, or a fetch subprocess a
REM    killed collection left behind, plus stray Playwright browsers that hold sockets and
REM    make the next fetch look like a network failure.
where powershell >nul 2>&1
if not errorlevel 1 (
  powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'serve\.py|ght\.cli|ght\.api|-m ght' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
  powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'Temp\\playwright' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
)

REM Give the OS a moment to release the socket before the new portal binds it.
timeout /t 2 /nobreak >nul

exit /b 0
