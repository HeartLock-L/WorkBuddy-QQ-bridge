@echo off
rem Read the project root FIRST, then pass an ABSOLUTE path when calling
rem start-astrbot.bat. Calling it by a bare relative name would make its own
rem %~dp0 point at the wrong directory.
setlocal
set "ROOT=%~dp0"

title AstrBot - reset dashboard password
cd /d "%ROOT%"

echo ============================================================
echo  AstrBot - reset dashboard password
echo.
echo  This script will:
echo    1. stop any AstrBot that is currently using port 6185
echo    2. restart AstrBot with a freshly generated password
echo.
echo  Watch the output below for this block:
echo.
echo      Initial username: astrbot
echo      Initial password: XXXXXXXXXXXXXXXXXXXXXXXX
echo.
echo  Then open  http://127.0.0.1:6185  and log in with it.
echo  The password is also saved in: AstrBot\data\logs\astrbot.log
echo ============================================================
echo.

if not exist "%ROOT%start-astrbot.bat" (
    echo [ERROR] start-astrbot.bat not found in %ROOT%
    pause
    exit /b 1
)

echo [1/2] stopping AstrBot on port 6185 ...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":6185" ^| findstr "LISTENING"') do (
    echo       killing PID %%p
    taskkill /F /PID %%p >nul 2>&1
)
ping -n 2 127.0.0.1 >nul

echo [2/2] starting AstrBot with --reset-password ...
echo.

call "%ROOT%start-astrbot.bat" --reset-password

echo.
echo ============================================================
echo  Scroll up and look for:   Initial password:
echo  Or search AstrBot\data\logs\astrbot.log
echo ============================================================
pause
