@echo off
title P104-100 Bandwidth Test
cd /d "%~dp0"
if not exist "bin\P104Gen2x4.exe" (
    echo [ERROR] bin\P104Gen2x4.exe not found - extract the whole zip first.
    pause
    exit /b 1
)
"bin\P104Gen2x4.exe" -bw
echo.
pause