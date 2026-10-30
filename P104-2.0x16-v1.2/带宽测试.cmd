@echo off
rem ============================================================
rem  P104-100 CUDA bandwidth test  (read only, needs NVIDIA driver)
rem  Pure ASCII + CRLF on purpose (see README, v1.0 mojibake lesson).
rem ============================================================
setlocal
cd /d "%~dp0"
title P104-100 CUDA bandwidth test

echo ============================================================
echo   P104-100  CUDA bandwidth test  (read only)
echo ============================================================
echo   Target  : H2D 1.4+ GB/s  (Gen2 x4)
echo   Gen1 x4 : H2D about 0.78 GB/s
echo   Note    : the link drops to Gen1 for about 10-20 s during the test;
echo             the tool then waits for the link to recover (up to 30 s).
echo             That temporary slowdown is normal.
echo.
"%~dp0bin\P104Gen2x4.exe" -bw
echo.
pause
