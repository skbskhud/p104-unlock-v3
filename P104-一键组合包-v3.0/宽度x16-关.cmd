@echo off
chcp 65001 >nul
title P104 LnkCap width restore
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set HERE=%~dp0
"%HERE%python\python.exe" "%HERE%tool\width16_hold.py" decl16-off
echo.
pause