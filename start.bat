@echo off
chcp 65001 >nul
title Music CN Tagger
cd /d "%~dp0"

echo Music CN Tagger
echo URL: http://localhost:5174
echo.
echo (Ctrl+C to stop)
echo.

python -X utf8 app.py

if errorlevel 1 (
    echo.
    echo [exited with error] make sure Python 3.10+ is on PATH and dependencies are installed:
    echo     pip install -r requirements.txt
    echo.
    pause
)
