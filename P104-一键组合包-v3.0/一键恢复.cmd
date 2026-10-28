@echo off
chcp 65001 >nul
title P104 一键恢复
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set BASE=D:\workplace2\dsh-workspace
set DIR=%BASE%\_scratch\directions\width16
"%BASE%\.python312\python.exe" "%DIR%\width16_hold.py" uninstall
echo.
pause
