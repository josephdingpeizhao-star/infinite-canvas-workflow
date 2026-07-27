@chcp 936 >nul
@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "CREATE_STOP=0"
if "%~1"=="" goto :arguments_ok
if /I "%~1"=="维护" set "CREATE_STOP=1"
if /I "%~1"=="维护" goto :arguments_ok
echo 参数无法识别。本文件支持以下两种用法：
echo   直接双击：默认模式只创建“无限画布工作台”
echo   创建桌面入口.bat 维护：同时创建维护者使用的停止入口
pause
exit /b 2

:arguments_ok
set "PYTHONW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined PYTHONW set "PYTHONW=%%~fI"
if not defined PYTHONW (
    echo 未找到 pythonw.exe，无法创建无黑窗启动入口。
    echo 请先安装本项目使用的 Python，再重新运行本文件。
    pause
    exit /b 1
)
if not exist "%PYTHONW%" (
    echo 找到的 pythonw.exe 无法使用：%PYTHONW%
    pause
    exit /b 1
)

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
set "START_SCRIPT=%~dp0canvas_launcher.py"
set "STOP_SCRIPT=%~dp0canvas_stop.py"
for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -NonInteractive -Command "[Environment]::GetFolderPath('Desktop')"`) do set "DESKTOP=%%I"
if not defined DESKTOP (
    echo 无法定位当前用户的桌面目录。
    pause
    exit /b 1
)

powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$shell=New-Object -ComObject WScript.Shell; $start=$shell.CreateShortcut((Join-Path $env:DESKTOP '无限画布工作台.lnk')); $start.TargetPath=$env:PYTHONW; $start.Arguments=([char]34+$env:START_SCRIPT+[char]34); $start.WorkingDirectory=$env:REPO_ROOT; $start.Description='无窗启动无限画布工作台'; $start.Save()"
if errorlevel 1 (
    echo 启动画布的桌面入口创建失败，请确认当前账户可以写入桌面。
    pause
    exit /b 1
)

if "%CREATE_STOP%"=="1" (
    powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$shell=New-Object -ComObject WScript.Shell; $stop=$shell.CreateShortcut((Join-Path $env:DESKTOP '停止画布工作台.lnk')); $stop.TargetPath=$env:PYTHONW; $stop.Arguments=([char]34+$env:STOP_SCRIPT+[char]34); $stop.WorkingDirectory=$env:REPO_ROOT; $stop.Description='安全停止无限画布工作台'; $stop.Save()"
    if errorlevel 1 (
        echo 维护者停止入口创建失败；启动入口已经创建。
        pause
        exit /b 1
    )
    echo 维护模式已创建“无限画布工作台”和“停止画布工作台”两个桌面入口。
) else (
    echo 默认模式只创建“无限画布工作台”桌面入口；没有创建或删除任何停止入口。
)
pause
exit /b 0
