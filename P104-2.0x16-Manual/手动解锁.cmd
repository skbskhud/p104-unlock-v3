@echo off
chcp 65001 >nul
title P104-100 Manual Unlock (KEEP THIS WINDOW OPEN)
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ERROR] Administrator rights required.
    echo         Right-click this file and choose "Run as administrator".
    echo.
    pause
    exit /b 1
)
echo ============================================================
echo   P104-100  MANUAL unlock (Gen2 x4 + width x16 declaration)
echo.
echo   * KEEP THIS WINDOW OPEN while you use the card.
echo   * Close this window / Ctrl+C  = stop holding (link drops back to Gen1).
echo   * No autostart task is installed in this manual build.
echo ============================================================
echo.
"bin\P104Gen2x4.exe"
echo.
echo Holder exited.
pause