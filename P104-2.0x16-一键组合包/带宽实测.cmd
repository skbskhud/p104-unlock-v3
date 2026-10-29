@echo off
chcp 65001 >nul
title P104-100 真实带宽实测
cd /d "%~dp0"
if not exist "bin\check-bw.cmd" (
    echo [错误] 找不到 bin\check-bw.cmd，请先完整解压整个压缩包。
    pause
    exit /b 1
)
call "bin\check-bw.cmd"
