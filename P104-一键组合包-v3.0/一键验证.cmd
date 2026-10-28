@echo off
chcp 65001 >nul
title P104 Verify
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set HERE=%~dp0
"%HERE%python\python.exe" "%HERE%tool\width16_hold.py" status
echo.
echo 判据: 声明 Gen2 / 声明 x16 / 当前链路 三项分开看; 补完电容后当前链路 width 应变成 16.
echo.
choice /c YN /n /m "Retrain PCIe link now? [Y/N] "
if errorlevel 2 goto end
"%HERE%python\python.exe" "%HERE%tool\width16_hold.py" retrain
:end
echo.
pause
