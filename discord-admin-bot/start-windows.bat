@echo off
setlocal
cd /d "%~dp0"
title Claude Discord Admin Bot

rem --- Find Python 3.11+ ---
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (where python >nul 2>nul && set "PY=python")
if not defined PY (
  echo Python is not installed.
  echo Install Python 3.11 or newer from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo Python 3.11 or newer is required. Get it from https://www.python.org/downloads/
  pause
  exit /b 1
)

rem --- One-time install ---
if not exist ".venv\Scripts\python.exe" (
  echo Setting up for the first time...
  %PY% -m venv .venv || (pause & exit /b 1)
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || (pause & exit /b 1)

rem --- First run (or setup quit before choosing a server): interactive setup ---
set "NEEDSETUP="
if not exist ".env" set "NEEDSETUP=1"
if exist ".env" (findstr /r /c:"^GUILD_IDS=[0-9]" ".env" >nul || set "NEEDSETUP=1")
if defined NEEDSETUP (
  ".venv\Scripts\python.exe" -m admin_bot setup || (pause & exit /b 1)
)

rem --- Run, restarting automatically if it crashes ---
:loop
echo Starting bot. Close this window to stop it.
".venv\Scripts\python.exe" -m admin_bot
if errorlevel 2 (pause & exit /b 2)
echo Bot stopped. Restarting in 10 seconds... (close the window to quit)
timeout /t 10 /nobreak >nul
goto loop
