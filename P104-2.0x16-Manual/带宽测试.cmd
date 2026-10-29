@echo off
chcp 65001 >nul
title P104-100 Bandwidth
cd /d "%~dp0"
"bin\P104Gen2x4.exe" -bw
echo.
pause