@echo off
setlocal
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title QQbot - offline checks
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] Project Python is missing.
    pause
    exit /b 1
)
if exist "%ROOT%\tests\test_*.py" (
    echo [INFO] Running the development regression suite.
    "%ROOT%\.venv\Scripts\python.exe" -B -m unittest discover -s "%ROOT%\tests" -p "test_*.py"
    if errorlevel 1 (
        echo [FAIL] Offline regression checks failed.
        pause
        exit /b 1
    )
) else (
    echo [INFO] Development tests are not included. Running deployment self-check only.
)
"%ROOT%\.venv\Scripts\python.exe" -B -u "%ROOT%\tests\bridge_selftest.py"
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
