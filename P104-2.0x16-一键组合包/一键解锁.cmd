@echo off
chcp 65001 >nul
title P104-100 一键解锁（Gen2 x4 + 宽度声明 x16）
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
    echo 需要管理员权限，正在请求提权……
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo ============================================================
echo   P104-100  PCIe 2.0 x4 解锁  +  宽度声明 x16
echo   开机自启（登录后约 60 秒）/ 常驻看门狗 / 失败自动重试
echo ============================================================
echo.
echo [1/4] 安装并注册开机任务……
"bin\P104Gen2x4.exe" -install
echo.
echo [2/4] 立即解锁一次……
"bin\P104Gen2x4.exe" -once
echo.
echo [3/4] 寄存器级检查……
"bin\P104Gen2x4.exe" -diag
echo.
echo [4/4] 状态……
"bin\P104Gen2x4.exe" -status
echo.
echo 完成。建议重启一次，让"开机自动解锁"生效。
echo 以后验证：一键验证.cmd    测真实带宽：带宽实测.cmd
echo.
pause
