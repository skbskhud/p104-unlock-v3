@echo off
rem ============================================================
rem  Uninstall the P104-100 Gen2 x4 tool.
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 uninstall

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
echo   Uninstall P104-100 Gen2 x4 tool
echo ============================================================
echo.
echo  This will:
echo    - stop and delete the scheduled task
echo      "P104 PCIe Gen2x4 Bring-up"
echo    - stop the hold process and unload the driver services
echo    - ask before deleting C:\Program Files\P104Gen2x4
echo.
echo  NOTE: the NVIDIA driver policy keys (RMPcieLinkSpeed /
echo  RmForceEnableGen2 / RmEnableGen2) are KEPT on purpose.
echo  Removing them makes a later manual unlock fail until the
echo  driver applies them again. To remove them as well, run:
echo    bin\P104Gen2x4.exe -uninstall -purgepolicy
echo.

echo [1/5] Ending the scheduled task ...
schtasks /end /tn "P104 PCIe Gen2x4 Bring-up" >nul 2>&1
echo [2/5] Deleting the scheduled task ...
schtasks /delete /tn "P104 PCIe Gen2x4 Bring-up" /F >nul 2>&1
echo [3/5] Stopping the hold process ...
taskkill /IM P104Gen2x4.exe /F >nul 2>&1
timeout /t 2 /nobreak >nul 2>&1
echo [4/5] Running the tool uninstaller ...
"%~dp0bin\P104Gen2x4.exe" -uninstall
echo.
echo [5/5] Delete the installed program folder?
echo        C:\Program Files\P104Gen2x4
choice /C YN /N /M "  Delete this folder now? [Y/N] "
if errorlevel 2 goto skiprm
rmdir /S /Q "C:\Program Files\P104Gen2x4"
if exist "C:\Program Files\P104Gen2x4" (
  echo   [WARN] folder still exists - close any running tool and delete it manually.
) else (
  echo   Folder deleted.
)
goto done

:skiprm
echo   Folder kept (you can delete it later by hand).

:done
echo.
echo Done. The card returns to the factory state (Gen1 x4) after reboot.
echo.
pause
