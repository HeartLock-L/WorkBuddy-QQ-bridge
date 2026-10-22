@echo off
title workbuddy_bridge self-test
cd /d "%~dp0"

echo ============================================================
echo  workbuddy_bridge ´¿Âß¼­×Ô¼ì
echo  Working dir: %cd%
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv python not found.
    pause
    exit /b 1
)

if not exist "test-workbuddy-bridge.py" (
    echo [ERROR] test-workbuddy-bridge.py not found.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -u "test-workbuddy-bridge.py"

echo.
echo ============================================================
echo  Done.
echo ============================================================
pause
