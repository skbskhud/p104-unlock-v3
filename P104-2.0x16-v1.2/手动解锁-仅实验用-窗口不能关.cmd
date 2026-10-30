@echo off
rem ============================================================
rem  EXPERIMENTAL ONLY - NOT for daily use.
rem  The hold process runs in the foreground: this window IS the
rem  hold process. Closing it stops the hold and the link falls
rem  back to Gen1 x4.
rem  For normal use (close the window, keep Gen2 x4, survive a
rem  reboot) use the ONE-CLICK script in this folder instead.
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 manual unlock - EXPERIMENTAL (window must stay open)

rem ---- administrator check ----
net session >nul 2>&1
if errorlevel 1 (
  echo.
  echo  [ERROR] Administrator rights are required.
  echo  Right-click this .cmd file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo ============================================================
echo   P104-100 manual unlock  (EXPERIMENTAL, no autostart)
echo ============================================================
echo.
echo  This window IS the hold process. Keep it open while you use
echo  the card; closing it stops the hold (link falls back to Gen1 x4).
echo  For normal use (close the window and keep Gen2 x4), use the
echo  ONE-CLICK script in this folder instead.
echo.
echo  Only one instance may touch the card at a time, so any
echo  running hold process is stopped first.
echo.

echo [1/2] Stopping a previous hold process (if any) ...
schtasks /end /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
taskkill /IM P104Gen2x4.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1
echo [2/2] Starting the hold process (reset budget: 1) ...
echo.
"%~dp0bin\P104Gen2x4.exe" -retry 1
echo.
echo Hold process exited - the link falls back to Gen1 x4.
echo.
pause