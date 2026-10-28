@echo off
chcp 65001 >nul
title P104 一键解锁 (Gen2 + 宽度x16)
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set BASE=D:\workplace2\dsh-workspace
set DIR=%BASE%\_scratch\directions\width16
"%BASE%\.python312\python.exe" "%DIR%\width16_hold.py" oneclick
echo.
pause
