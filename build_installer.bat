@echo off
:: Build OroStarMail-Setup-1.4.7.exe using Inno Setup.
:: Run AFTER build_exe.bat has completed successfully.
:: Requires Inno Setup 6 installed from: jrsoftware.org/isinfo.php
setlocal

set REPO=%~dp0
set ISS=%REPO%installer.iss
set OUTDIR=%REPO%installer_output

:: Find ISCC.exe — check common install paths then fall back to registry
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"

:: Registry fallback — works for any 6.x version including 6.7.x
if "%ISCC%"=="" (
    for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v "InstallLocation" 2^>nul') do set "ISCC=%%bISCC.exe"
)
if "%ISCC%"=="" (
    for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Inno Setup 6_is1" /v "InstallLocation" 2^>nul') do set "ISCC=%%bISCC.exe"
)

if "%ISCC%"=="" (
    echo ERROR: Inno Setup 6 not found. Make sure it is installed.
    exit /b 1
)
echo Using: %ISCC%

if not exist "%REPO%dist\OroStarMail\OroStarMail.exe" (
    echo ERROR: dist\OroStarMail\OroStarMail.exe not found.
    echo Run build_exe.bat first.
    exit /b 1
)

echo =^> Building installer...
"%ISCC%" "%ISS%"
if errorlevel 1 ( echo ERROR: Inno Setup compilation failed & exit /b 1 )

echo.
echo Done:
dir "%OUTDIR%\*.exe"
