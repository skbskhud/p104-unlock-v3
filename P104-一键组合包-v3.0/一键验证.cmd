@echo off
chcp 65001 >nul
title P104 一键验证
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set BASE=D:\workplace2\dsh-workspace
set DIR=%BASE%\_scratch\directions\width16
set PY=%BASE%\.python312\python.exe
"%PY%" "%DIR%\width16_hold.py" status
echo.
echo 说明: 声明 Gen2 / 声明 x16 / 当前链路 三个分别看。
echo       补完电容后当前链路 width 应该从 4 变 16。
echo.
choice /c YN /n /m "Retrain PCIe link now? [Y/N] "
if errorlevel 2 goto end
"%PY%" "%DIR%\width16_hold.py" retrain
:end
echo.
pause
