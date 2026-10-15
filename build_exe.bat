@echo off
:: Build OroStarMail.exe for Windows.
:: Run from the repo root: build_exe.bat
:: Requires Python 3.10+ on PATH.
setlocal enabledelayedexpansion

set REPO=%~dp0
set VENV=%REPO%.venv
set DIST=%REPO%dist
set EXE=%DIST%\OroStarMail\OroStarMail.exe

echo =^> Checking virtual environment...
if not exist "%VENV%\Scripts\python.exe" (
    echo Creating .venv...
    python -m venv "%VENV%"
    if errorlevel 1 ( echo ERROR: python -m venv failed & exit /b 1 )
)

echo =^> Installing dependencies...
"%VENV%\Scripts\pip" install --quiet -r "%REPO%requirements.txt"
if errorlevel 1 ( echo ERROR: pip install failed & exit /b 1 )
"%VENV%\Scripts\pip" install --quiet pyinstaller
if errorlevel 1 ( echo ERROR: pip install pyinstaller failed & exit /b 1 )

echo =^> Building EXE...
cd /d "%REPO%"
"%VENV%\Scripts\pyinstaller" OroStarMail.spec --noconfirm
if errorlevel 1 ( echo ERROR: PyInstaller failed & exit /b 1 )

if not exist "%EXE%" (
    echo ERROR: EXE not found at %EXE%
    exit /b 1
)

echo.
echo Done: %EXE%
dir "%EXE%"
