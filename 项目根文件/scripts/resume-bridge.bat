@echo off
setlocal
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title QQbot - resume bridge
if exist "%ROOT%\bridge.paused" del /q "%ROOT%\bridge.paused"
echo QQ bridge resumed.
pause
