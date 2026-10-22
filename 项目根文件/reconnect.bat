@echo off
title QQbot reconnect
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv python not found.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -u "reconnect.py" %*

echo.
pause
