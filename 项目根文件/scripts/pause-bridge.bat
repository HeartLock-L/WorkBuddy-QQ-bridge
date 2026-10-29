@echo off
setlocal
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title QQbot - pause bridge
> "%ROOT%\bridge.paused" echo paused
echo QQ bridge paused. Run scripts\resume-bridge.bat to resume.
pause
