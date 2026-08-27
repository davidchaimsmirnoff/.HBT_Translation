@echo off
title Sefaria Commentary Downloader
cd /d "%~dp0"

REM Prefer the py launcher, fall back to whatever python is on PATH.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 sefaria_server.py
) else (
    python sefaria_server.py
)

if errorlevel 1 (
    echo.
    echo Server exited with an error. Read the message above.
    pause
)
