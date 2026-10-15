@echo off
:: AMail migration cleanup — removes all OroStarMail traces from this PC.
:: Run this BEFORE installing AMail.
:: Must be run as Administrator.
setlocal enabledelayedexpansion

echo ============================================
echo  AMail Cleanup — removing OroStarMail
echo ============================================
echo.

:: -- 1. Uninstall OroStarMail if installed --------------------------------
echo Checking for OroStarMail installation...
set UNINSTALLER=
for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\OroStarMail_is1" /v "UninstallString" 2^>nul') do set UNINSTALLER=%%b
if "%UNINSTALLER%"=="" (
    for /f "tokens=2*" %%a in ('reg query "HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\OroStarMail_is1" /v "UninstallString" 2^>nul') do set UNINSTALLER=%%b
)

if not "%UNINSTALLER%"=="" (
    echo Found OroStarMail — uninstalling silently...
    "%UNINSTALLER%" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
    timeout /t 5 /nobreak >nul
    echo Done.
) else (
    echo OroStarMail not found in Programs — skipping uninstall.
)

:: -- 2. Kill process if somehow still running -----------------------------
taskkill /f /im OroStarMail.exe >nul 2>&1

:: -- 3. Remove leftover Program Files folder ------------------------------
if exist "C:\Program Files\OroStarMail" (
    echo Removing C:\Program Files\OroStarMail ...
    rd /s /q "C:\Program Files\OroStarMail"
)
if exist "C:\Program Files (x86)\OroStarMail" (
    echo Removing C:\Program Files (x86)\OroStarMail ...
    rd /s /q "C:\Program Files (x86)\OroStarMail"
)

:: -- 4. Remove user data folder (mail cache) ------------------------------
set USERDATA=%USERPROFILE%\.orostarmail
if exist "%USERDATA%" (
    echo Removing user data folder: %USERDATA% ...
    rd /s /q "%USERDATA%"
)

:: -- 5. Remove registry settings ------------------------------------------
echo Removing registry settings...
reg delete "HKCU\Software\OroStarMail" /f >nul 2>&1

:: -- 6. Remove Start Menu shortcuts ---------------------------------------
set STARTMENU=%APPDATA%\Microsoft\Windows\Start Menu\Programs\OroStarMail
if exist "%STARTMENU%" (
    echo Removing Start Menu shortcuts...
    rd /s /q "%STARTMENU%"
)

:: -- 7. Remove Desktop shortcut -------------------------------------------
if exist "%USERPROFILE%\Desktop\OroStarMail.lnk" (
    echo Removing desktop shortcut...
    del /f "%USERPROFILE%\Desktop\OroStarMail.lnk"
)

:: -- 8. Remove Windows Credential Manager entries -------------------------
echo Removing stored credentials...
for /f "tokens=2*" %%a in ('cmdkey /list ^| findstr /i "Target:"') do (
    echo %%b | findstr /i "orostarmail" >nul 2>&1
    if not errorlevel 1 cmdkey /delete:%%b >nul 2>&1
)

echo.
echo ============================================
echo  Cleanup complete. You can now install AMail.
echo ============================================
pause
