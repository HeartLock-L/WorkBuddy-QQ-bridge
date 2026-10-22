@echo off
title QQbot doctor
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv python not found.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -u "doctor.py" %*

echo.
pause
