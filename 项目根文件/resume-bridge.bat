@echo off
rem ==== 恢复 QQ 中转 ====
rem 删掉维护标记，插件下一条群消息就会照常处理（不用重启 AstrBot）。
setlocal
set "ROOT=%~dp0"

title QQbot - resume bridge

cd /d "%ROOT%"

if exist "%ROOT%bridge.paused" (
    del /q "%ROOT%bridge.paused"
    echo.
    echo   QQ 中转已恢复。
) else (
    echo.
    echo   本来就是正常运行状态，无需恢复。
)
echo.
pause
