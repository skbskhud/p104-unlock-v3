@echo off
rem ============================================================
rem  Manual unlock without autostart: the hold process runs in the
rem  foreground, keep this window open. Closing the window (or
rem  Ctrl+C) stops the hold and the link falls back to Gen1 x4.
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 manual unlock (keep this window open)

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
echo   P104-100  manual unlock  (no autostart)
echo ============================================================
echo.
echo  KEEP THIS WINDOW OPEN while you use the card.
echo  This window IS the hold process: when you see the link is Gen2,
echo  you may leave it open in the background and use the card.
echo  Close it (or Ctrl+C) only when you are done - then the link
echo  falls back to Gen1 x4 (this is normal, not a failure).
echo.
echo  Only one instance may touch the card at a time, so any
echo  running hold process is stopped first.
echo.
echo  If you want to CLOSE the window and keep Gen2, use the
echo  ONE-CLICK script instead: it installs a background task, so
echo  the window can be closed.
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
