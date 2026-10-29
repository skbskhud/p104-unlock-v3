@echo off
chcp 65001 >nul
title P104-100 状态验证
cd /d "%~dp0"
if not exist "bin\P104Gen2x4.exe" (
    echo [错误] 找不到 bin\P104Gen2x4.exe，请先完整解压整个压缩包。
    pause
    exit /b 1
)
echo ============================================================
echo   P104-100 PCIe 状态验证（只读，不需要管理员）
echo ============================================================
echo.
"bin\P104Gen2x4.exe" -status
echo.
echo ------------------------------------------------------------
echo  重要：
echo   - RESULT 行（Gen2 x4 UNLOCKED）= 真实链路状态，可信。
echo   - GPU-Z / nvidia-smi 在空闲时可能显示 "1.1 / x4"，
echo     这是这张矿卡的驱动上报值，不可信；请看 RESULT 或测速。
echo   - 要看寄存器细节：右键此文件 → 以管理员身份运行（会跑 -diag）。
echo ------------------------------------------------------------
echo.
pause
