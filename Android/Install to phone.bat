@echo off
title Install LocalHBT to phone
cd /d "%~dp0"

REM adb normally lives in the SDK that Android Studio installed, but honour an
REM explicit ANDROID_SDK_ROOT / ANDROID_HOME first, and fall back to PATH.
set "ADB="
if defined ANDROID_SDK_ROOT if exist "%ANDROID_SDK_ROOT%\platform-tools\adb.exe" set "ADB=%ANDROID_SDK_ROOT%\platform-tools\adb.exe"
if not defined ADB if defined ANDROID_HOME if exist "%ANDROID_HOME%\platform-tools\adb.exe" set "ADB=%ANDROID_HOME%\platform-tools\adb.exe"
if not defined ADB if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" set "ADB=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
if not defined ADB for %%I in (adb.exe) do if not "%%~$PATH:I"=="" set "ADB=%%~$PATH:I"

if not defined ADB (
  echo Could not find adb.exe.
  echo Looked in ANDROID_SDK_ROOT, ANDROID_HOME, %LOCALAPPDATA%\Android\Sdk\platform-tools and PATH.
  echo.
  echo You can skip this script entirely: copy Android\build\LocalHBT.apk to the
  echo phone and tap it.
  echo.
  pause
  exit /b 1
)

if not exist "build\LocalHBT.apk" (
  echo No APK yet - run "Build APK.bat" first.
  echo.
  pause
  exit /b 1
)

echo Using  %ADB%
echo.

REM wait-for-device blocks for ever when nothing is plugged in, which just looks
REM like a hung window. Ask adb what it can see instead, and say so.
"%ADB%" start-server >nul 2>&1
set "SERIAL="
set "UNAUTH="
for /f "skip=1 tokens=1,2" %%A in ('"%ADB%" devices') do (
  if "%%B"=="device" set "SERIAL=%%A"
  if "%%B"=="unauthorized" set "UNAUTH=%%A"
)

if defined UNAUTH (
  echo The phone is connected but has not authorised this computer.
  echo Unlock it and accept the "Allow USB debugging?" prompt, then run this again.
  echo.
  pause
  exit /b 1
)

if not defined SERIAL (
  echo No phone detected over USB.
  echo.
  echo   1. Settings -^> About phone -^> tap "Build number" seven times
  echo   2. Settings -^> System -^> Developer options -^> USB debugging  ON
  echo   3. Plug the phone in and set the USB mode to File transfer
  echo.
  echo Or skip USB altogether: copy Android\build\LocalHBT.apk to the phone
  echo and tap it.
  echo.
  pause
  exit /b 1
)

echo Found phone %SERIAL%
echo.
"%ADB%" -s %SERIAL% install -r "build\LocalHBT.apk"
if errorlevel 1 goto failed

echo.
echo Installed. Look for "LocalHBT" in the app drawer.
echo.
pause
exit /b 0

:failed
echo.
echo adb could not install the APK. If the phone shows an "Allow USB debugging?"
echo prompt, accept it and run this again.
echo.
pause
exit /b 1
