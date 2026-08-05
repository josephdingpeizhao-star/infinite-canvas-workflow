chcp 936 >nul
@echo off
setlocal EnableExtensions DisableDelayedExpansion

set "SOURCE_DIR=%USERPROFILE%\.infinite-canvas"
set "TARGET_DIR=%~dp0credentials"
if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"
if errorlevel 1 goto :failed

call :copy_with_backup "%SOURCE_DIR%\render-credentials.json" "%TARGET_DIR%\render-credentials.json"
if errorlevel 1 goto :failed
call :copy_with_backup "%SOURCE_DIR%\canvas-agent.json" "%TARGET_DIR%\canvas-agent.json"
if errorlevel 1 goto :failed

echo 凭据已按字节导出到 deploy\credentials；如目标原已存在，旧文件保存在 .bak。
pause
exit /b 0

:copy_with_backup
if not exist "%~1" (
    echo 未找到待导出的凭据文件：%~nx1
    exit /b 1
)
if exist "%~2" (
    copy /b /y "%~2" "%~2.bak" >nul
    if errorlevel 1 exit /b 1
)
copy /b /y "%~1" "%~2" >nul
exit /b %errorlevel%

:failed
echo 导出未完成；请确认凭据文件存在并且当前账户可以写入 deploy\credentials。
pause
exit /b 1
