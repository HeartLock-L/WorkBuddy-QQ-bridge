@echo off
rem Capture the project root BEFORE any cd.
rem %~dp0 degenerates to the CURRENT DIRECTORY when this script is invoked by
rem a bare relative name (e.g. "call start-astrbot.bat"), so it must be read
rem first and then reused everywhere.
setlocal
set "ROOT=%~dp0"

title AstrBot

rem Force UTF-8 I/O. Without this, logging a reply that contains characters
rem outside the console codepage (GBK) can raise UnicodeEncodeError and kill
rem the process silently.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%ROOT%AstrBot"

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found:
    echo         %PY%
    pause
    exit /b 1
)

set "WEBUI=%ROOT%AstrBot\dashboard\dist"
if not exist "%WEBUI%\index.html" set "WEBUI=%ROOT%AstrBot\dashboard"
if not exist "%WEBUI%\index.html" (
    echo [WARN] dashboard assets not found, AstrBot will try to download them.
    set "WEBUI="
)

echo ============================================================
echo  AstrBot  -  Dashboard http://127.0.0.1:6185
echo  Username : astrbot
echo  Password : printed on the first start (look for "Initial password")
echo ============================================================
echo.

if defined WEBUI (
    "%PY%" main.py --webui-dir "%WEBUI%" %*
) else (
    "%PY%" main.py %*
)

echo.
echo AstrBot exited.
pause
