@echo off
setlocal
title Cryptogam Digitizationizer 2000
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"

rem ---------------------------------------------------------------------------
rem  Double-click to start the Digitizationizer.
rem  First run: creates a private Python environment (.venv) and installs the
rem  packages listed in requirements.txt (needs internet, takes a few minutes).
rem  Later runs: starts straight away, reinstalling only if requirements.txt
rem  has changed since the last install.
rem  Closing this window stops the app.
rem ---------------------------------------------------------------------------

set "VENV_PY=.venv\Scripts\python.exe"
set "STAMP=.venv\requirements.installed.txt"

if exist "%VENV_PY%" goto :check_reqs

echo.
echo  First-time setup: creating the Python environment...
echo.
set "BASE_PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "BASE_PY=py -3"
if not defined BASE_PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1 && set "BASE_PY=python"
)
if not defined BASE_PY (
    echo  ERROR: Python 3.9 or newer was not found on this computer.
    echo.
    echo  Install it from https://www.python.org/downloads/
    echo  ^(tick "Add python.exe to PATH" during install^), then run this again.
    echo.
    pause
    exit /b 1
)
%BASE_PY% -m venv .venv
if errorlevel 1 (
    echo  ERROR: could not create the .venv folder.
    pause
    exit /b 1
)

:check_reqs
if exist "%STAMP%" (
    fc /b "requirements.txt" "%STAMP%" >nul 2>&1 && goto :run
)
echo.
echo  Installing / updating packages ^(this can take a few minutes^)...
echo.
"%VENV_PY%" -m pip install --disable-pip-version-check -q --upgrade pip
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: package install failed. Check the internet connection and try again.
    pause
    exit /b 1
)
copy /y "requirements.txt" "%STAMP%" >nul

:run
"%VENV_PY%" launch.py %*
if errorlevel 1 (
    echo.
    echo  The app stopped with an error ^(see the messages above^).
    pause
)
endlocal
