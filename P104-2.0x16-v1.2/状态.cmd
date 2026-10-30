@echo off
rem ============================================================
rem  P104-100 PCIe link status  (read only, no admin needed)
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 link status

echo ============================================================
echo   P104-100  PCIe link status  (read only)
echo ============================================================
echo.
"%~dp0bin\P104Gen2x4.exe" -check
echo.
echo  How to read the RESULT line:
echo    - contains "Gen2 x4"  = unlocked and working
echo    - contains "Gen1 x4"  = not unlocked yet
echo    - "not held" / "unknown" = hold process not running, or data stale (wait a few seconds)
echo.
pause
