chcp 936 >nul
@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SOURCE_DIR=%~dp0credentials"
set "TARGET_DIR=%USERPROFILE%\.infinite-canvas"
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if errorlevel 1 goto :failed

call :copy_with_backup "%SOURCE_DIR%\render-credentials.json" "%TARGET_DIR%\render-credentials.json"
if errorlevel 1 goto :failed
call :copy_with_backup "%SOURCE_DIR%\canvas-agent.json" "%TARGET_DIR%\canvas-agent.json"
if errorlevel 1 goto :failed

call "%~dp0..\launcher\创建桌面入口.bat" 维护
if errorlevel 1 goto :failed

echo 安装完成。桌面已创建启动和停止两个工作台入口。
pause
exit /b 0

:copy_with_backup
if not exist "%~1" (
    echo 部署包中缺少凭据文件：%~nx1
    exit /b 1
)
if exist "%~2" (
    copy /b /y "%~2" "%~2.bak" >nul
    if errorlevel 1 exit /b 1
)
copy /b /y "%~1" "%~2" >nul
exit /b %errorlevel%

:failed
echo 安装未完成；如原凭据已被备份，可从个人目录中的 .bak 恢复。
pause
exit /b 1
