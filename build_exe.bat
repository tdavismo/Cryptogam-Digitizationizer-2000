@echo off
setlocal

echo ============================================================
echo  Herbarium Packet Segmenter - Build EXE
echo ============================================================
echo.

REM --- Locate Python ----------------------------------------------------------
REM  Try the 'py' launcher first (always available after a standard Windows
REM  install), then fall back to 'python' if someone has that on their PATH.

set PY=
where py >nul 2>&1 && set PY=py
if "%PY%"=="" (
    where python >nul 2>&1 && set PY=python
)
if "%PY%"=="" (
    echo ERROR: Python was not found.
    echo.
    echo  Please install Python from https://www.python.org/downloads/
    echo  During installation, tick "Add Python to PATH".
    echo  Then re-run this script.
    pause
    exit /b 1
)

echo  Using Python: %PY%
%PY% --version
echo.

REM --- Install dependencies ---------------------------------------------------
echo [1/3] Installing Python dependencies...
%PY% -m pip install --upgrade customtkinter Pillow opencv-python numpy pyinstaller
if errorlevel 1 (
    echo.
    echo ERROR: Dependency install failed. See output above.
    pause
    exit /b 1
)

echo.
echo [2/3] Building executable...
echo       This may take a few minutes - please wait.
echo.

%PY% -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name "HerbariumSegmenter" ^
  --collect-all customtkinter ^
  segmenter_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Build failed. See output above for details.
    pause
    exit /b 1
)

echo.
echo [3/3] Done!
echo.
echo  The executable is at:
echo    %~dp0dist\HerbariumSegmenter.exe
echo.
echo  You can copy that single .exe file to any Windows computer and run it.
echo  No Python installation is needed on the target computer.
echo.
pause
