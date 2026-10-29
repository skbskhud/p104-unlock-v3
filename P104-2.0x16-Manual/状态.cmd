@echo off
chcp 65001 >nul
title P104-100 Status
cd /d "%~dp0"
echo ============================================================
echo   Status + bandwidth (read-only)
echo ============================================================
echo.
"bin\P104Gen2x4.exe" -status
echo.
"bin\P104Gen2x4.exe" -bw
echo.
pause