@echo off
setlocal
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title QQbot - doctor
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] Project Python is missing.
    pause
    exit /b 1
)
"%ROOT%\.venv\Scripts\python.exe" -u "%SCRIPTS%doctor.py" %*
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
