@echo off
title P104-100 Uninstall
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Administrator rights are required.
    echo         RIGHT-CLICK uninstall.cmd and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)
echo [1/3] Stopping autostart task...
schtasks /end /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
echo [2/3] Removing task, drivers and registry policy...
"bin\P104Gen2x4.exe" -uninstall
echo [3/3] Restoring registers...
"bin\P104Gen2x4.exe" -off
echo.
echo Uninstalled. Reboot is recommended.
pause