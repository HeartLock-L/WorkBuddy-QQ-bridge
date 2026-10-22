@echo off
title fix-bat-encoding
cd /d "%~dp0"

echo ============================================================
echo   .bat encoding repair: UTF-8 -^> GBK + CRLF, strip code-page lines
echo   Working dir: %cd%
echo ============================================================
echo.

if not exist "fix-bat-encoding.py" (
    echo [ERROR] fix-bat-encoding.py not found.
    pause
    exit /b 1
)

echo Originals are backed up to a dated backup folder before any change.
echo.
echo Scanning *.bat in this folder ...
echo.

".venv\Scripts\python.exe" -u "fix-bat-encoding.py"

echo.
echo ============================================================
echo   Done. If a .bat still prints a broken word like "cho",
echo   that file was not saved as UTF-8: restore it from the backup
echo   and re-edit it in an editor that keeps GBK.
echo ============================================================
pause
