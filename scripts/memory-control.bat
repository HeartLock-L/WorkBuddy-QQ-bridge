@echo off
setlocal
chcp 65001 >nul
set "SCRIPTS=%~dp0"
for %%I in ("%SCRIPTS%..") do set "ROOT=%%~fI"
cd /d "%ROOT%"
title QQbot - memory control
set "PYTHON_EXE=%ROOT%\.venv\Scripts\python.exe"
set "PYTHON_ARG="
if not exist "%PYTHON_EXE%" goto try_py
"%PYTHON_EXE%" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto ready
:try_py
set "PYTHON_EXE=py"
set "PYTHON_ARG=-3"
py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto ready
set "PYTHON_EXE=python"
set "PYTHON_ARG="
python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>&1
if not errorlevel 1 goto ready
echo 未找到可用的 Python，请先检查运行环境。
pause
exit /b 1
:ready
if not "%~1"=="" goto arguments
echo 1. 开始本轮记忆整理
echo 2. 当前批次结束后停止
echo 3. 查看整理状态
choice /c 123 /n /m "请选择："
if errorlevel 3 goto status
if errorlevel 2 goto stop
"%PYTHON_EXE%" %PYTHON_ARG% -B -u "%SCRIPTS%memory-control.py" --start
goto finished
:stop
"%PYTHON_EXE%" %PYTHON_ARG% -B -u "%SCRIPTS%memory-control.py" --stop
goto finished
:status
"%PYTHON_EXE%" %PYTHON_ARG% -B -u "%SCRIPTS%memory-control.py" --status
goto finished
:arguments
"%PYTHON_EXE%" %PYTHON_ARG% -B -u "%SCRIPTS%memory-control.py" %*
:finished
set "RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %RESULT%
