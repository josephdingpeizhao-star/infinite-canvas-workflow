@chcp 936 >nul
@echo off
setlocal EnableExtensions DisableDelayedExpansion
set "PYTHONW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined PYTHONW set "PYTHONW=%%~fI"
if not defined PYTHONW (
    echo 未找到 pythonw.exe，无法停止无限画布工作台。
    echo 请先安装本项目使用的 Python，再运行本文件。
    pause
    exit /b 1
)
if not exist "%PYTHONW%" (
    echo 找到的 pythonw.exe 无法使用：%PYTHONW%
    pause
    exit /b 1
)
start "" /D "%~dp0" "%PYTHONW%" "%~dp0launcher\canvas_stop.py"
exit /b 0
