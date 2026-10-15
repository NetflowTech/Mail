
@echo off
setlocal
cd /d "%~dp0"

set "REPO=%CD%"
set "VENV=%REPO%\.venv"
set "PYTHON=%VENV%\Scripts\python.exe"

echo.
echo Checking project folder...
echo "%REPO%"
echo.

if not exist "%REPO%\requirements.txt" (
    echo ERROR: requirements.txt not found.
    pause
    exit /b 1
)

echo Checking virtual environment...

if exist "%VENV%" (
    echo Removing incomplete virtual environment...
    rmdir /s /q "%VENV%"
)

py -m venv "%VENV%"
if errorlevel 1 (
    echo ERROR: Could not create virtual environment.
    pause
    exit /b 1
)

echo Installing dependencies...
"%PYTHON%" -m pip install --upgrade pip
if errorlevel 1 goto :error

"%PYTHON%" -m pip install -r "%REPO%\requirements.txt"
if errorlevel 1 goto :error

"%PYTHON%" -m pip install pyinstaller
if errorlevel 1 goto :error

echo Building Windows application...
"%PYTHON%" -m PyInstaller "%REPO%\AMail.spec" --noconfirm
if errorlevel 1 goto :error

echo.
echo Build completed. Check the dist folder.
pause
exit /b 0

:error
echo.
echo ERROR: Build process failed.
pause
exit /b 1
