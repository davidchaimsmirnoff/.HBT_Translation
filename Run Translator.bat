@echo off
title LocalHBT Translator
cd /d "%~dp0"

REM Prefer the py launcher, fall back to whatever python is on PATH.
where py >nul 2>&1
if %errorlevel%==0 (
    py -3 server.py
) else (
    python server.py
)

if errorlevel 1 (
    echo.
    echo Server exited with an error. Read the message above.
    pause
)
