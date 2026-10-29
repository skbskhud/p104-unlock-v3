@echo off
chcp 65001 >nul
title P104-100 卸载恢复
cd /d "%~dp0"
if not exist "bin\uninstall.cmd" (
    echo [错误] 找不到 bin\uninstall.cmd，请先完整解压整个压缩包。
    pause
    exit /b 1
)
call "bin\uninstall.cmd"
