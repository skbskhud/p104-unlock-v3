@echo off
rem ============================================================
rem  P104-100 PCIe Gen2 x4 unlock - one click script  (v1.1)
rem
rem  This file is intentionally pure ASCII + CRLF.
rem  v1.0 shipped cmd files as UTF-8 and switched the console code page; every
rem  message mixed with sc.exe GBK output turned into mojibake.
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 Gen2 x4 unlock v1.1

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
echo   P104-100  PCIe Gen2 x4 unlock   v1.1
echo ============================================================
echo.
echo  This script will:
echo    1. install the tool to  C:\Program Files\P104Gen2x4
echo    2. register a scheduled task
echo       (boot+90s / logon+120s / self-heal every 5 minutes)
echo    3. start the unlock now and wait up to 90 seconds
echo.
echo  Keep this window open until it finishes.
echo.

echo [1/3] Installing ...
"%~dp0bin\P104Gen2x4.exe" -install
if errorlevel 1 (
  echo.
  echo  [ERROR] Install failed.
  echo  Make sure bin\P104Gen2x4.exe exists and that you are administrator.
  echo.
  pause
  exit /b 1
)
echo.

echo [2/3] Starting the scheduled task ...
schtasks /run /tn "P104 PCIe Gen2x4 Bring-up"
if errorlevel 1 (
  echo  [WARN] schtasks /run failed - starting the installed exe directly ...
  start "" "C:\Program Files\P104Gen2x4\P104Gen2x4.exe" -quiet -retry 1
)
echo.

echo [3/3] Waiting for the Gen2 link (up to 90 seconds) ...
echo.
set WAIT=0

:waitloop
"%~dp0bin\P104Gen2x4.exe" -check >"%TEMP%\p104_check.txt" 2>&1
findstr /C:"UNLOCKED" "%TEMP%\p104_check.txt" >nul 2>&1
if not errorlevel 1 goto success
set /a WAIT+=5
if %WAIT% GEQ 90 goto failed
timeout /t 5 /nobreak >nul 2>&1
echo    ... still waiting, %WAIT% seconds
goto waitloop

:success
del "%TEMP%\p104_check.txt" >nul 2>&1
echo.
echo ============================================================
echo   SUCCESS - the link is now Gen2 x4.
echo ============================================================
echo.
"%~dp0bin\P104Gen2x4.exe" -check
echo.
echo  Next steps:
echo    - run the bandwidth test script in this folder
echo      (target: H2D 1.4+ GB/s; Gen1 x4 is about 0.78 GB/s)
echo    - do not run the manual unlock script while the task is active
echo    - the scheduled task keeps the link up and self-heals
echo.
pause
exit /b 0

:failed
del "%TEMP%\p104_check.txt" >nul 2>&1
echo.
echo ============================================================
echo   FAILED - the link did not reach Gen2 within 90 seconds.
echo ============================================================
echo.
echo  Try in this order:
echo    1. run this script one more time
echo    2. run the STATUS script in this folder and read the RESULT line
echo    3. shut down, unplug the power cable for 30 seconds (AC power off),
echo       boot again and run this script
echo.
echo  If the card disappeared from Device Manager, do step 3 first.
echo.
pause
exit /b 1
