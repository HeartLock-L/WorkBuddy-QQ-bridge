@echo off
rem ==== 暂停 QQ 中转（管理员要占用桌面端窗口改东西时用）====
rem 背景：WorkBuddy Gateway 复用桌面端的活动会话，QQ 中转和桌面端窗口抢同一条会话。
rem       中转在跑的时候，你在窗口里发的指令会被群里那一轮 run 顶掉，看着像"被无视"。
rem 用法：双击本脚本 -> 群里不再有任何回复/主动开口 -> 放心在窗口里改代码/下指令。
rem       改完双击 resume-bridge.bat 恢复。切完立即生效，不用重启 AstrBot。
setlocal
set "ROOT=%~dp0"

title QQbot - pause bridge

cd /d "%ROOT%"

> "%ROOT%bridge.paused" echo paused

echo.
echo   QQ 中转已暂停：群里消息不再回复，也不会主动开口。
echo   恢复请双击 resume-bridge.bat
echo.
pause
