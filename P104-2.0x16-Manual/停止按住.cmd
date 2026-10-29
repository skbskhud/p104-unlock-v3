@echo off
chcp 65001 >nul
title P104-100 Stop Holder
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Administrator rights required.
    pause
    exit /b 1
)
echo Stopping the hold process...
taskkill /F /IM P104Gen2x4.exe >nul 2>&1
echo done. Link will fall back to Gen1 (normal).
pause