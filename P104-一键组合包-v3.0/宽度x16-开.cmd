@echo off
chcp 65001 >nul
title P104 LnkCap width x16 (declaration only)
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set HERE=%~dp0
echo.
echo  This only changes the DECLARED max width (LnkCap maxwidth = x16).
echo  It does NOT change the actual link width or bandwidth.
echo  Do NOT run a Gen2 retrain right after this (it may trigger an x16 commit and drop the card).
echo.
"%HERE%python\python.exe" "%HERE%tool\width16_hold.py" decl16
echo.
pause