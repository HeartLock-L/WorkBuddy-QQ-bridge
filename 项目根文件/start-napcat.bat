@echo off
title NapCat - QQ Bot

setlocal
cd /d "%~dp0NapCat\NapCatShell"

set "QQEXE=%~dp0NapCat\QQNT\Files\QQ.exe"
if not exist "%QQEXE%" (
    echo [ERROR] QQ.exe not found:
    echo         %QQEXE%
    pause
    exit /b 1
)

set NAPCAT_PATCH_PACKAGE=%cd%\qqnt.json
set NAPCAT_LOAD_PATH=%cd%\loadNapCat.js
set NAPCAT_INJECT_PATH=%cd%\NapCatWinBootHook.dll
set NAPCAT_LAUNCHER_PATH=%cd%\NapCatWinBootMain.exe
set NAPCAT_MAIN_PATH=%cd%\napcat.mjs
set NAPCAT_MAIN_PATH=%NAPCAT_MAIN_PATH:\=/%

echo (async () =^> {await import("file:///%NAPCAT_MAIN_PATH%")})() > "%NAPCAT_LOAD_PATH%"

rem 默认不预设账号：首次启动会出二维码，手机扫码登录即可。
rem NapCat 会记住登录过的号，之后启动直接进，不必再扫。
rem 想指定账号：start-napcat.bat -q 123456789
if "%~1"=="" (
    set "NAP_EXTRA="
    echo [info] no account preset - scan the QR code (first run only)
) else (
    set "NAP_EXTRA=%*"
)

echo ============================================================
echo  NapCat launcher
echo  QQ   : %QQEXE%
echo  WebUI: http://127.0.0.1:6099/webui
echo  AstrBot reverse WS target: ws://127.0.0.1:6199/ws
echo ============================================================
echo.

"%NAPCAT_LAUNCHER_PATH%" "%QQEXE%" "%NAPCAT_INJECT_PATH%" %NAP_EXTRA%

echo.
echo NapCat exited.
pause
