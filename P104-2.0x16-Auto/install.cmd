@echo off
title P104-100 Install and Unlock (Gen2 x4 + Width x16)
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Administrator rights are required.
    echo         Close this window, then RIGHT-CLICK install.cmd
    echo         and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)
echo ============================================================
echo   P104-100  PCIe 2.0 x4 unlock  +  Width x16 declaration
echo   Auto-start: boot +60s / logon +15s / every minute self-heal
echo ============================================================
echo.
echo [1/3] Installing and registering autostart task...
"bin\P104Gen2x4.exe" -install
echo.
echo [2/3] Starting the hold process (unlock now)...
schtasks /run /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
echo        waiting 10 seconds...
timeout /t 10 /nobreak >nul
echo.
echo [3/3] Status + bandwidth...
"bin\P104Gen2x4.exe" -status
"bin\P104Gen2x4.exe" -bw
echo.
echo Done. Reboot once to activate the boot-time auto unlock.
echo If the result above is Gen2 x4 (1.5 GB/s) - it is working.
echo.
pause