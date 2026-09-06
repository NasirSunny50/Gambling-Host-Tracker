@echo off
REM ============================================================================
REM Make sure a usable Python (3.11+) exists, INSTALLING it if it does not, and
REM leave the interpreter to use in GHT_PY for whatever called this.
REM
REM Returns 0 with GHT_PY set on success. Returns 1 only when Python could not be
REM made to exist at all - which on a normal machine means no internet, since the
REM install falls back all the way to downloading the official installer.
REM
REM Nothing here needs a person: no admin prompt (everything is per-user), no
REM clicks. The one thing it cannot do is refresh THIS window's PATH for a Python
REM it just installed, so it re-reads PATH from the registry itself and, failing
REM that, points straight at the freshly-installed exe.
REM ============================================================================
set "GHT_PY="

REM --- Already have one on PATH? ---------------------------------------------
call :try py -3
if defined GHT_PY goto :ok
call :try python
if defined GHT_PY goto :ok

echo.
echo [..] Python 3.11+ was not found on this PC. Installing it automatically...

REM --- Preferred: winget, Microsoft's own package manager --------------------
where winget >nul 2>&1
if %errorlevel%==0 (
  echo [..] Installing Python via winget ^(no clicks needed^)...
  winget install -e --id Python.Python.3.12 --silent --scope user --accept-package-agreements --accept-source-agreements --disable-interactivity
  call :locate
  if defined GHT_PY goto :ok
)

REM --- Fallback: the official installer straight from python.org -------------
echo [..] Downloading the official Python installer from python.org...
set "PYURL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
set "PYEXE=%TEMP%\ght-python-setup.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; try { Invoke-WebRequest -Uri '%PYURL%' -OutFile '%PYEXE%' } catch { exit 1 }"
if not exist "%PYEXE%" (
  echo.
  echo [X] Could not download Python. This is almost always no internet connection.
  echo     Connect to the internet and run this again.
  exit /b 1
)
echo [..] Installing Python, per-user, no admin needed. This takes a minute or two...
"%PYEXE%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%PYEXE%" >nul 2>&1
call :locate
if defined GHT_PY goto :ok

echo.
echo [X] Python was installed but this window cannot see it yet.
echo     Close this window and run the file again - a fresh window will find it,
echo     and the rest of the setup will carry on from here.
exit /b 1

:ok
echo [ok] Using Python: %GHT_PY%
exit /b 0

REM ---------------------------------------------------------------------------
:try
REM Candidate launcher in %*; adopt it into GHT_PY only if it runs and is 3.11+.
%* -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)" >nul 2>&1
if %errorlevel%==0 set "GHT_PY=%*"
exit /b 0

:locate
REM Find a just-installed Python without a new window. First re-read PATH from the
REM registry - PrependPath / winget write it there but not into this session - then,
REM if that still does not surface it, put the exe's own folder on this session's PATH
REM and use the bare `python`. GHT_PY is only ever `py -3` or `python`, never a full
REM path: a quoted path travels badly through `venv` and later reads back broken.
call :refreshpath
call :try py -3
if defined GHT_PY exit /b 0
call :try python
if defined GHT_PY exit /b 0
set "GHT_PYDIR="
for /d %%d in ("%LocalAppData%\Programs\Python\Python3*") do if exist "%%d\python.exe" set "GHT_PYDIR=%%d"
if not defined GHT_PYDIR for /d %%d in ("%ProgramFiles%\Python3*") do if exist "%%d\python.exe" set "GHT_PYDIR=%%d"
if defined GHT_PYDIR (
  set "PATH=%GHT_PYDIR%;%GHT_PYDIR%\Scripts;%PATH%"
  call :try python
)
exit /b 0

:refreshpath
REM Rebuild this session's PATH from the machine and user PATH the installer just
REM wrote, so a `py` / `python` it added resolves here without reopening the window.
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "GHT_MPATH=%%B"
for /f "skip=2 tokens=2,*" %%A in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "GHT_UPATH=%%B"
set "PATH=%GHT_MPATH%;%GHT_UPATH%;%PATH%"
exit /b 0
