@echo off
title P104-100 Status
cd /d "%~dp0"
if not exist "bin\P104Gen2x4.exe" (
    echo [ERROR] bin\P104Gen2x4.exe not found - extract the whole zip first.
    pause
    exit /b 1
)
echo ============================================================
echo   P104-100 PCIe status  (declared width + actual width)
echo ============================================================
echo.
"bin\P104Gen2x4.exe" -status
echo.
echo ============================================================
echo   Bandwidth check (CUDA, no Python needed)
echo ============================================================
echo.
"bin\P104Gen2x4.exe" -bw
echo.
pause