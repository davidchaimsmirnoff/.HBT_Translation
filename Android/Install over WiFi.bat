@echo off
setlocal enabledelayedexpansion
title Install LocalHBT over WiFi
cd /d "%~dp0"

REM Installs the APK with no cable, using Android 11+ Wireless debugging. The
REM older "adb tcpip 5555" trick needs a USB connection to switch the phone into
REM network mode in the first place, which defeats the point; pairing codes do
REM not, so this never needs the cable at all.

set "TARGETFILE=%~dp0.wireless-target"

REM ---------------------------------------------------------------- find adb
set "ADB="
if defined ANDROID_SDK_ROOT if exist "%ANDROID_SDK_ROOT%\platform-tools\adb.exe" set "ADB=%ANDROID_SDK_ROOT%\platform-tools\adb.exe"
if not defined ADB if defined ANDROID_HOME if exist "%ANDROID_HOME%\platform-tools\adb.exe" set "ADB=%ANDROID_HOME%\platform-tools\adb.exe"
if not defined ADB if exist "%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe" set "ADB=%LOCALAPPDATA%\Android\Sdk\platform-tools\adb.exe"
if not defined ADB for %%I in (adb.exe) do if not "%%~$PATH:I"=="" set "ADB=%%~$PATH:I"

if not defined ADB (
  echo Could not find adb.exe.
  echo Looked in ANDROID_SDK_ROOT, ANDROID_HOME, %LOCALAPPDATA%\Android\Sdk\platform-tools and PATH.
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
"%ADB%" start-server >nul 2>&1

REM ------------------------------------------------- already connected?
call :findwireless
if defined WSERIAL (
  echo Already connected to !WSERIAL!
  goto install
)

REM ------------------------------------------------- reconnect to last target
set "LAST="
if exist "%TARGETFILE%" set /p LAST=<"%TARGETFILE%"
if defined LAST (
  echo Trying the address this phone used last time: !LAST!
  "%ADB%" connect !LAST! >nul 2>&1
  call :findwireless
  if defined WSERIAL (
    echo Connected to !WSERIAL!
    goto install
  )
  echo   that address did not answer - the port changes every time Wireless
  echo   debugging is turned off and on, so it usually needs re-entering.
  echo.
)

REM ------------------------------------------------- ask what to do
echo On the phone: Settings -^> Developer options -^> Wireless debugging  ON
echo.
echo   [1] First time on this computer - pair with a code
echo   [2] Already paired - just reconnect
echo   [3] Quit
echo.
set "CHOICE="
set /p CHOICE=Choose 1, 2 or 3:
if "!CHOICE!"=="3" exit /b 0
if "!CHOICE!"=="2" goto justconnect
if not "!CHOICE!"=="1" goto justconnect

REM ------------------------------------------------- pair
echo.
echo On the phone, tap "Pair device with pairing code". It shows an address
echo and a six digit code. Both are only valid while that dialog is open.
echo.
set "PADDR="
set /p PADDR=Pairing address from the phone (IP:PORT):
if not defined PADDR goto cancelled
set "PCODE="
set /p PCODE=Six digit pairing code:
if not defined PCODE goto cancelled

echo.
"%ADB%" pair !PADDR! !PCODE!
echo.
echo Now close that dialog. The main Wireless debugging screen shows a
echo DIFFERENT port - that is the one to connect to.
echo.

:justconnect
echo.
echo Use the IP address and port shown on the Wireless debugging screen.
echo.
echo If the phone is on the same WiFi as this PC, use the address it shows.
echo If it is not, use the phone's Tailscale address with that same port:
echo   100.98.198.75:PORT
echo.
set "CADDR="
set /p CADDR=Connect address (IP:PORT):
if not defined CADDR goto cancelled

echo.
"%ADB%" connect !CADDR!
call :findwireless
if not defined WSERIAL (
  echo.
  echo Could not connect to !CADDR!
  echo.
  echo   - Wireless debugging has to stay ON; turning it off changes the port
  echo   - The port on the main screen is not the pairing port
  echo   - If you skipped pairing, run this again and choose [1]
  echo.
  pause
  exit /b 1
)
(echo !CADDR!)>"%TARGETFILE%"
echo Connected to !WSERIAL!

REM ------------------------------------------------- install
:install
echo.
"%ADB%" -s !WSERIAL! install -r "build\LocalHBT.apk"
if errorlevel 1 (
  echo.
  echo The install failed. If the phone has been rebooted since pairing, turn
  echo Wireless debugging off and on and run this again.
  echo.
  pause
  exit /b 1
)

echo.
echo Installed over WiFi. Look for "LocalHBT" in the app drawer.
echo.
pause
exit /b 0

:cancelled
echo.
echo Cancelled - nothing was installed.
echo.
pause
exit /b 1

REM A wireless device shows up as ADDRESS:PORT rather than a USB serial number,
REM which is what tells the two apart in adb's device list.
:findwireless
set "WSERIAL="
REM cmd mangles the quoting when a for /f command block contains both a quoted
REM program path and a pipe, which surfaces as "The system cannot find the path
REM specified". Land the output in a file and search that instead.
"%ADB%" devices >"%TEMP%\hbt_devices.txt" 2>nul
for /f "tokens=1,2" %%A in ('findstr /r /c:":[0-9][0-9]*.*device" "%TEMP%\hbt_devices.txt"') do set "WSERIAL=%%A"
del "%TEMP%\hbt_devices.txt" >nul 2>&1
goto :eof
