@echo off
setlocal
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title AstrBot - reset dashboard password
if not exist "%ROOT%\.venv\Scripts\python.exe" (
    echo [ERROR] Project Python is missing.
    pause
    exit /b 1
)
if not exist "%SCRIPTS%reset-astrbot-password.py" (
    echo [ERROR] Safe password reset helper is missing.
    pause
    exit /b 1
)
echo This stops only the verified AstrBot instance belonging to this project.
"%ROOT%\.venv\Scripts\python.exe" -u "%SCRIPTS%reset-astrbot-password.py"
if errorlevel 1 (
    echo Password reset cancelled. No new AstrBot will be started.
    pause
    exit /b 1
)
call "%ROOT%\start-astrbot.bat" --reset-password
