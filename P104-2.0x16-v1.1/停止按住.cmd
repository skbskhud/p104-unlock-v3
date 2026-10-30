@echo off
rem ============================================================
rem  Stop the hold process and restore the original link registers.
rem  The link is no longer held at Gen2 after this.
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 stop holding

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
echo   Stop holding and restore the original link
echo ============================================================
echo.
echo  The original register values are written back; Gen2 is no longer held.
echo  The scheduled task stays installed and will unlock again
echo  on its next trigger.
echo.

echo [1/4] Ending the scheduled task ...
schtasks /end /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
echo [2/4] Waiting 3 seconds ...
timeout /t 3 /nobreak >nul 2>&1
echo [3/4] Stopping any P104Gen2x4 process ...
taskkill /IM P104Gen2x4.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1
echo [4/4] Restoring the original registers ...
"%~dp0bin\P104Gen2x4.exe" -off
if errorlevel 1 echo   [WARN] restore returned an error (card missing or snapshot lost).
echo.
echo Done. The original link registers have been restored.
echo.
pause
