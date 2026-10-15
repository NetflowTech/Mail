
@echo off
setlocal
cd /d "%~dp0"

set "REPO=%~dp0"
set "ISS=%REPO%installer.iss"
set "OUTDIR=%REPO%installer_output"
set "ISCC="
if exist "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"

echo Searching for Inno Setup 6...

:: Standard installation paths
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
)

if not defined ISCC if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" (
    set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
)

:: Check registry installation paths
if not defined ISCC (
    for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v InstallLocation 2^>nul') do (
        if /i "%%A"=="InstallLocation" set "ISCC=%%BISCC.exe"
    )
)

if not defined ISCC (
    for /f "tokens=2,*" %%A in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v InstallLocation 2^>nul') do (
        if /i "%%A"=="InstallLocation" set "ISCC=%%BISCC.exe"
    )
)

:: Check common alternative location
if not defined ISCC if exist "C:\InnoSetup\ISCC.exe" (
    set "ISCC=C:\InnoSetup\ISCC.exe"
)

if not defined ISCC (
    echo ERROR: Could not locate ISCC.exe.
    echo Please locate ISCC.exe on your computer.
    pause
    exit /b 1
)

echo Found compiler: "%ISCC%"

if not exist "%ISS%" (
    echo ERROR: installer.iss not found.
    pause
    exit /b 1
)

if not exist "%REPO%dist\AMail\AMail.exe" (
    echo ERROR: Application executable not found.
    echo Run build_exe.bat successfully first.
    pause
    exit /b 1
)

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

echo.
echo Building installer...
"%ISCC%" "%ISS%"
if errorlevel 1 (
    echo ERROR: Inno Setup compilation failed.
    pause
    exit /b 1
)

echo.
echo Installer build finished.
dir "%OUTDIR%\*.exe"
pause
