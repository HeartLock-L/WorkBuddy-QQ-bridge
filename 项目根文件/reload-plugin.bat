@echo off
rem ==== AstrBot 插件重载（逃生通道）====
rem 用途：workbuddy_bridge 加载失败时，热重载 watcher 会跟着一起死，
rem       文件改好了也救不回来。双击这个脚本手动重载一次即可复活。
rem 不带参数 = 重载 workbuddy_bridge；也可以拖/传一个插件目录名。
setlocal
set "ROOT=%~dp0"

title AstrBot - reload plugin

cd /d "%ROOT%"

set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found:
    echo         %PY%
    pause
    exit /b 1
)

if "%~1"=="" (
    "%PY%" "%ROOT%reload-plugin.py"
) else (
    "%PY%" "%ROOT%reload-plugin.py" "%~1"
)

echo.
pause
