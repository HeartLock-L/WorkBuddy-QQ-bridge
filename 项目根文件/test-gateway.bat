@echo off
title WorkBuddy Gateway self-test
cd /d "%~dp0"

echo ============================================================
echo  WorkBuddy Gateway self-test
echo  Working dir: %cd%
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] venv python not found.
    pause
    exit /b 1
)

if not exist "test-workbuddy-gateway.py" (
    echo [ERROR] test-workbuddy-gateway.py not found.
    pause
    exit /b 1
)

if not exist "workbuddy-gateway.env" (
    echo [WARN] workbuddy-gateway.env is missing.
    echo.
)

echo --- running test ---
echo.

".venv\Scripts\python.exe" -u "test-workbuddy-gateway.py" %*

echo.
echo ============================================================
echo  Done. Copy the lines above and send them back.
echo ============================================================
pause
