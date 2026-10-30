@echo off
chcp 65001 >nul
taskkill /IM P104Gen2x4.exe /F >nul 2>&1
echo Holder stopped. Link will fall back to Gen1.
pause