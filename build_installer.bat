@echo off
:: Build OroStarMail-Setup-1.4.7.exe using Inno Setup.
:: Run AFTER build_exe.bat has completed successfully.
:: Requires Inno Setup 6 installed from: jrsoftware.org/isinfo.php
setlocal

set REPO=%~dp0
set ISS=%REPO%installer.iss
set OUTDIR=%REPO%installer_output

:: Find ISCC.exe (Inno Setup command-line compiler)
set ISCC=
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe
if exist "C:\Program Files\Inno Setup 6\ISCC.exe"       set ISCC=C:\Program Files\Inno Setup 6\ISCC.exe

if "%ISCC%"=="" (
    echo ERROR: Inno Setup 6 not found.
    echo Download it from: https://jrsoftware.org/isinfo.php
    exit /b 1
)

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
