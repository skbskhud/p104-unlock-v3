@echo off
chcp 65001 >nul
title P104 Width x16 ON (DANGEROUS)
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
set HERE=%~dp0
echo.
echo ============================================================
echo  WARNING / READ BEFORE CONTINUE
echo.
echo  Enabling the x16 width declaration while the physical lanes
echo  / AC coupling capacitors are NOT ready will make the hardware
echo  attempt an x16 commit; measured result: THE GPU DROPS OFF THE
echo  BUS and needs a full power cycle to recover.
echo.
echo  Only continue after the physical layer is verified.
echo ============================================================
echo.
choice /c YN /n /m "Continue? [Y/N] "
if errorlevel 2 goto end
"%HERE%python\python.exe" "%HERE%tool\width16_hold.py" width-on --force
:end
echo.
pause